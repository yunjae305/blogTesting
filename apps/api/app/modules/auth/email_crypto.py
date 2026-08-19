"""이메일을 저장 시 암호화한다 — 비밀번호와 달리 단방향 해시로는 부족하기 때문이다.

이메일은 세 가지로 쓰인다: 로그인 시 정확 일치 조회, 중복 가입 차단(유니크), 그리고
화면 표시·(추후) 이메일 발송. 앞의 둘만이면 비밀번호처럼 단방향 해시로 충분하지만,
셋째 때문에 원문을 되살릴 수 있어야 한다. 그래서 한 값을 둘로 나눈다:

- **블라인드 인덱스** ``email_hash`` = HMAC-SHA256(index_key, 정규화 이메일).
  결정적이라 조회·유니크 인덱스에 쓰고, 키가 없으면 되돌릴 수 없어(단방향) DB만 유출돼도
  흔한 이메일을 사전 대입으로 알아내지 못한다.
- **가역 암호문** ``email_enc`` = AES-256-GCM(enc_key, 정규화 이메일), 매번 랜덤 논스.
  표시·발송을 위해 복호화한다. GCM은 인증 암호화라 변조도 잡아낸다.

v3(2026-08-09)부터 암호문에 key id가 들어간다: ``v3:<kid>:<base64(논스+암호문)>``.
Windows Server 이전을 앞두고 "암호화 키는 절대 바꿀 수 없다"는 제약을 없애기 위한
것이다. ``EMAIL_ENC_KEY_2``(정확히 32바이트의 canonical base64url)를 추가하면 새
암호화는 그 키로 쓰면서 옛 kid·v2·AAD 없는 암호문도 계속 읽고, 전체 재암호화와 옛 키
제거는 ``scripts/rotate_email_key.py``의 절차를 따른다. v3 키는 비밀에서 바로 쓰지 않고
PBKDF2(문구일 때)·HKDF(kid별 분리)로 유도한다 — salt 없는 SHA-256 1회 유도가 사전
대입에 약하다는 지적(docs/Windows-Server-보안-배포.md §1)을 새 형식에서 메운 것이다.

**EMAIL_INDEX_KEY는 회전 대상이 아니다.** 블라인드 인덱스는 결정적이어야 조회·유니크가
동작하므로, 인덱스 키를 바꾸려면 전체 emailHash 재계산 마이그레이션이 따로 필요하다.

암복호는 리포지토리 경계 안에서만 일어난다(도메인 ``User.email``은 계속 평문). 키는
비밀번호·LLM 키와 같은 급의 비밀이며, 운영에서는 외부 vault에서 환경변수로 주입한다.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

EMAIL_ENC_KEY_ENV = "EMAIL_ENC_KEY"
EMAIL_INDEX_KEY_ENV = "EMAIL_INDEX_KEY"

# GCM 표준 논스 길이. 논스는 암호문 앞에 붙여 함께 저장한다 — 비밀이 아니라 재사용만
# 피하면 되고, 매 암호화마다 새로 뽑는다.
_NONCE_BYTES = 12
_CIPHERTEXT_V2 = "v2:"
_CIPHERTEXT_V3 = "v3:"
# 리포지토리가 "버전이 있어 그대로 둘 암호문"을 판별할 때 쓴다. 버전 있는 암호문의
# 재암호화(키 회전)는 읽기 경로가 아니라 scripts/rotate_email_key.py만 한다.
VERSIONED_CIPHERTEXT_PREFIXES = (_CIPHERTEXT_V2, _CIPHERTEXT_V3)

# 사람이 고른 문구를 오프라인 사전 대입에서 지키는 것은 반복 KDF다(OWASP의
# PBKDF2-HMAC-SHA256 권장 횟수). 기동 때 비밀당 1회만 치르고 프로세스 안에서 캐시한다.
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT = b"blog-it|email|kdf|v3"

# EMAIL_ENC_KEY(=kid 1) 또는 EMAIL_ENC_KEY_<n>(n≥2, 앞자리 0 없이). 이 밖의
# EMAIL_ENC_KEY* 이름에 값이 들어 있으면 조용히 무시하지 않고 설정 오류로 만든다 —
# 오타가 "회전을 마쳤다"는 착각으로 이어지면 안 된다.
_ENC_KEY_NAME = re.compile(r"EMAIL_ENC_KEY(?:_([1-9]\d*))?")
_V3_KID = re.compile(r"[1-9]\d{0,8}")
# POSTING_CREDENTIALS_KEY와 같은 규칙: 32바이트 키의 canonical base64url 한 표기만 인정.
_BASE64URL_32 = re.compile(r"[A-Za-z0-9_-]{43}=?")


def _aad_v2(context: str) -> bytes:
    return f"blog-it|email|v2|{context}".encode("utf-8")


def _aad_v3(kid: int, context: str) -> bytes:
    # kid까지 AAD에 넣어 암호문을 다른 kid로 옮겨 붙이는 것도 인증 실패로 만든다.
    return f"blog-it|email|v3|kid={kid}|{context}".encode("utf-8")


class EmailCryptoConfigError(RuntimeError):
    """이메일 암호화 키가 설정되지 않았거나 형식이 틀렸다. LLM 키가 없을 때처럼 시작을
    멈추게 한다 — 평문으로 조용히 물러나면 피드백을 받은 바로 그 문제(평문 저장)로
    되돌아간다."""


@dataclass(frozen=True)
class EncKey:
    """암호화 키 하나. ``key``는 v3용으로 유도된 32바이트. ``legacy_key``는 kid 1에만
    있는 원래 유도 키(sha256) — v2·AAD 없는 옛 암호문을 여는 데 쓴다."""

    kid: int
    key: bytes
    legacy_key: bytes | None = None


class EmailCipher:
    """블라인드 인덱스와 가역 암호문을 함께 만든다. 리포지토리에 주입해 쓴다.

    키를 kid별로 여러 개 가질 수 있고, 암호화는 항상 가장 큰 kid(활성 키)로 한다.
    복호화는 암호문에 적힌 kid의 키를 찾아 쓰므로 새 키를 추가해도 옛 암호문이 계속
    읽힌다 — 이것이 "옛 키로 읽고 새 키로 쓴 뒤, 전부 재암호화한 다음에만 옛 키를
    제거한다"는 회전 절차의 전제다."""

    def __init__(self, enc_key: bytes, index_key: bytes):
        # 단일 키 생성자 — 기존 호출·테스트 호환. enc_key가 kid 1이 된다.
        self._setup(
            [EncKey(kid=1, key=_derive_v3_key(enc_key, 1), legacy_key=enc_key)],
            index_key,
        )

    @classmethod
    def from_key_ring(cls, keys: Sequence[EncKey], index_key: bytes) -> "EmailCipher":
        cipher = cls.__new__(cls)
        cipher._setup(list(keys), index_key)
        return cipher

    def _setup(self, keys: list[EncKey], index_key: bytes) -> None:
        if not keys:
            raise ValueError("at least one enc key is required")
        if not index_key:
            raise ValueError("index_key must not be empty")
        kids = [entry.kid for entry in keys]
        if len(set(kids)) != len(kids):
            raise ValueError("enc key ids must be unique")
        for entry in keys:
            if entry.kid < 1:
                raise ValueError("enc key id must be a positive integer")
            if len(entry.key) != 32:
                raise ValueError("enc_key must be 32 bytes for AES-256")
        self._aead_by_kid = {entry.kid: AESGCM(entry.key) for entry in keys}
        self._active_kid = max(kids)
        legacy = next(
            (entry.legacy_key for entry in keys if entry.kid == 1 and entry.legacy_key),
            None,
        )
        self._legacy_aead = AESGCM(legacy) if legacy is not None else None
        self._index_key = index_key

    @property
    def active_kid(self) -> int:
        """새 암호화에 쓰는 key id. 회전 스크립트가 잔여 검증에 쓴다."""
        return self._active_kid

    def blind_index(self, normalized_email: str) -> str:
        """조회·유니크용 결정적 키. 같은 이메일은 늘 같은 값이 나온다."""
        return hmac.new(
            self._index_key, normalized_email.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def encrypt(self, normalized_email: str, *, context: str = "") -> str:
        kid = self._active_kid
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead_by_kid[kid].encrypt(
            nonce, normalized_email.encode("utf-8"), _aad_v3(kid, context)
        )
        encoded = base64.b64encode(nonce + ciphertext).decode("ascii")
        return f"{_CIPHERTEXT_V3}{kid}:{encoded}"

    def needs_rewrap(self, token: str) -> bool:
        """활성 키의 v3 형식이 아니면 참 — 키 회전(재암호화) 대상이라는 뜻이다."""
        return not token.startswith(f"{_CIPHERTEXT_V3}{self._active_kid}:")

    def decrypt(self, token: str, *, context: str = "") -> str:
        if token.startswith(_CIPHERTEXT_V3):
            kid_text, separator, encoded = token[len(_CIPHERTEXT_V3) :].partition(":")
            if not separator or not _V3_KID.fullmatch(kid_text):
                raise ValueError("email ciphertext is malformed")
            kid = int(kid_text)
            aead = self._aead_by_kid.get(kid)
            if aead is None:
                # 옛 키를 너무 일찍 지웠거나(회전 절차 위반) 다른 환경의 키 설정이다.
                suffix = "" if kid == 1 else f"_{kid}"
                raise ValueError(
                    f"email ciphertext uses key id {kid} but "
                    f"{EMAIL_ENC_KEY_ENV}{suffix} is not configured"
                )
            aad: bytes | None = _aad_v3(kid, context)
        elif token.startswith(_CIPHERTEXT_V2):
            encoded = token[len(_CIPHERTEXT_V2) :]
            aead = self._require_legacy_aead("v2")
            aad = _aad_v2(context)
        else:
            encoded = token
            aead = self._require_legacy_aead("legacy")
            aad = None

        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("email ciphertext is malformed") from error
        if len(raw) < _NONCE_BYTES + 16:
            raise ValueError("email ciphertext is malformed")
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return aead.decrypt(nonce, ciphertext, aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            # 키가 바뀌었거나 값이 변조됐다. 조용히 빈 문자열을 주면 엉뚱한 계정으로
            # 이어질 수 있으니 드러낸다.
            raise ValueError(
                "email ciphertext failed authentication (wrong key or tampered)"
            ) from error

    def _require_legacy_aead(self, kind: str) -> AESGCM:
        if self._legacy_aead is None:
            raise ValueError(
                f"{kind} email ciphertext needs the original {EMAIL_ENC_KEY_ENV} "
                "(kid 1), which is not configured"
            )
        return self._legacy_aead


def _derive_key(secret: str) -> bytes:
    """비밀 문자열을 32바이트 키로 유도한다(기존 방식 그대로). v2·AAD 없는 옛 암호문과
    블라인드 인덱스가 이 키로 만들어져 있어 바꿀 수 없다 — salt 없는 1회 해시라는 약점은
    v3 키 유도(_derive_v3_key)와 키 회전이 대신 메운다."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _canonical_base64url_key(secret: str) -> bytes | None:
    """정확히 32바이트를 canonical base64url로 적은 값이면 그 바이트를, 아니면 None."""
    if not _BASE64URL_32.fullmatch(secret):
        return None
    try:
        decoded = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 32:
        return None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    return decoded if canonical == secret.rstrip("=") else None


@lru_cache(maxsize=64)
def _passphrase_ikm(secret: str) -> bytes:
    """사람이 고른 문구는 반복 KDF로 늘인다. 무작위 32바이트 키(base64url)는 이 비용이
    필요 없어 _key_material이 먼저 걸러낸다."""
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ITERATIONS
    )


def _key_material(secret: str) -> bytes:
    decoded = _canonical_base64url_key(secret)
    return decoded if decoded is not None else _passphrase_ikm(secret)


def _derive_v3_key(material: bytes, kid: int) -> bytes:
    """kid까지 섞어 유도한다 — 같은 비밀을 실수로 두 kid에 써도 키가 달라지고,
    v2 키(sha256 직접 사용)와도 분리된다."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"blog-it|email|enc|v3|kid={kid}".encode("utf-8"),
    ).derive(material)


def _collect_enc_secrets(source: Mapping[str, str]) -> dict[int, str]:
    """EMAIL_ENC_KEY(_n) 값을 kid별로 모은다. 잘못된 이름·kid 충돌은 설정 오류다."""
    secrets_by_kid: dict[int, str] = {}
    for name, value in source.items():
        if not name.startswith(EMAIL_ENC_KEY_ENV):
            continue
        matched = _ENC_KEY_NAME.fullmatch(name)
        if matched is None:
            if value:
                raise EmailCryptoConfigError(
                    f"{name}은(는) 이메일 암호화 키 이름이 아닙니다. {EMAIL_ENC_KEY_ENV} "
                    f"또는 {EMAIL_ENC_KEY_ENV}_2처럼 앞자리 0 없는 번호만 붙일 수 있습니다."
                )
            continue
        if not value:
            continue
        kid = int(matched.group(1)) if matched.group(1) else 1
        if secrets_by_kid.get(kid, value) != value:
            raise EmailCryptoConfigError(
                f"kid {kid}의 이메일 암호화 키가 서로 다른 값으로 두 번 설정됐습니다 "
                f"({EMAIL_ENC_KEY_ENV}와 {EMAIL_ENC_KEY_ENV}_1은 같은 키입니다)."
            )
        secrets_by_kid[kid] = value
    return secrets_by_kid


def email_cipher_from_env(env: Mapping[str, str] | None = None) -> EmailCipher:
    """환경변수에서 이메일 암호화기를 만든다. 키가 없으면 시작을 멈춘다.

    - ``EMAIL_ENC_KEY``: kid 1. 기존 배포와 같은 임의 문자열을 허용한다(v2 호환).
    - ``EMAIL_ENC_KEY_<n>``(n≥2): 회전용 새 키. 정확히 32바이트의 canonical base64url만
      허용한다 — 새로 만드는 키까지 약한 문구일 이유가 없다.
    - 가장 큰 번호가 활성 키가 되어 새 암호화에 쓰인다.
    """
    source = env if env is not None else os.environ
    secrets_by_kid = _collect_enc_secrets(source)
    index_secret = source.get(EMAIL_INDEX_KEY_ENV)

    missing = []
    if not secrets_by_kid:
        missing.append(EMAIL_ENC_KEY_ENV)
    if not index_secret:
        missing.append(EMAIL_INDEX_KEY_ENV)
    if missing:
        raise EmailCryptoConfigError(
            "이메일을 암호화하려면 " + ", ".join(missing) + " 환경변수가 필요합니다. "
            "운영에서는 외부 vault로 주입하고, 로컬 개발 설정은 .env.example을 참고하세요."
        )

    if index_secret in secrets_by_kid.values():
        logger.warning(
            "EMAIL_INDEX_KEY가 이메일 암호화 키와 같은 값입니다 — 두 키는 서로 다른 "
            "비밀이어야 한쪽 유출의 피해가 분리됩니다."
        )

    keys: list[EncKey] = []
    for kid, secret in sorted(secrets_by_kid.items()):
        if kid == 1:
            keys.append(
                EncKey(
                    kid=1,
                    key=_derive_v3_key(_key_material(secret), 1),
                    legacy_key=_derive_key(secret),
                )
            )
            continue
        decoded = _canonical_base64url_key(secret)
        if decoded is None:
            raise EmailCryptoConfigError(
                f"{EMAIL_ENC_KEY_ENV}_{kid}은(는) 정확히 32바이트를 canonical base64url로 "
                "인코딩한 값이어야 합니다. 생성 예: python -c \"import base64,secrets; "
                "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()"
                ".rstrip('='))\""
            )
        keys.append(EncKey(kid=kid, key=_derive_v3_key(decoded, kid)))
    return EmailCipher.from_key_ring(keys, _derive_key(index_secret))
