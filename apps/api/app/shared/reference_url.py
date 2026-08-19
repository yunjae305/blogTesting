"""외부 AI에 전달할 사용자 참고 URL의 공통 보안 판정."""

import ipaddress
import socket
from urllib.parse import parse_qsl, urlparse


_SENSITIVE_PARAMETER_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "credential",
        "id_token",
        "key",
        "passwd",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


def is_public_reference_url(value: str) -> bool:
    """공개 도메인의 HTTP(S) URL이며 자격증명·비밀 파라미터가 없으면 ``True``.

    새 입력 검증뿐 아니라 provider 전송 직전에도 사용한다. 과거 저장 문서나 서버 내부
    조립 경로가 검증을 우회했더라도 사설 주소·서명 URL을 외부 AI에 보내지 않는다.
    """

    try:
        parsed = urlparse(value)
        # 잘못된 포트(`:abc`, `:99999`)는 hostname 접근만으로는 드러나지 않는다.
        _ = parsed.port
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in value)
        or "\\" in value
        or "%" in hostname
        or "." not in hostname
        or parsed.hostname != hostname
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        return False

    # URL Context는 공개 웹 주소만 지원한다. 참고자료는 사용자가 판별하기 어려운 IP literal
    # 대신 공개 도메인 이름만 받는다.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    try:
        # WHATWG URL 파서는 127.1, 0177.0.0.1, 0x7f.0.0.1도 127.0.0.1로 해석한다.
        # ipaddress는 이 legacy 표기를 거부하므로 inet_aton으로 한 번 더 잡는다.
        socket.inet_aton(hostname)
    except OSError:
        pass
    else:
        return False

    for parameter_text in (parsed.query, parsed.fragment):
        for key, _value in parse_qsl(parameter_text, keep_blank_values=True):
            normalized_key = key.strip().lower().replace("-", "_")
            if (
                normalized_key in _SENSITIVE_PARAMETER_KEYS
                or normalized_key.endswith(("_token", "_secret", "_signature", "_key"))
            ):
                return False
    return True
