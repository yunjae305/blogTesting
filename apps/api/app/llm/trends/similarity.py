"""Keyword signatures used for trend dedupe, history, and diversity selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha1
from typing import Iterable, Sequence

from .normalizer import normalize_keyword
from .text import clean_text, is_low_quality_keyword

_TOKEN = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣]+")
_COUNTER = re.compile(r"^\d+[가-힣]{1,2}$")
_LEADING_YEAR = re.compile(r"^(?:19|20)\d{2}$")

_PARTICLE_ENDINGS = ("으로", "에서", "에게", "과", "와", "은", "는", "을", "를", "의")

_ENTITY_DESCRIPTORS = {
    "개봉",
    "개봉일",
    "공개",
    "신작",
    "영화",
    "예고",
    "예고편",
    "일정",
    "시작",
    "소식",
    "운영",
    "개장",
    "2026",
}

_WEATHER_CLUSTER = {"장마", "폭염", "집중호우", "호우", "무더위", "날씨"}


@dataclass(frozen=True)
class KeywordSignature:
    normalized: str
    tokens: tuple[str, ...]
    token_set_signature: str
    cluster_id: str


def _trim_particle(word: str) -> str:
    for ending in _PARTICLE_ENDINGS:
        if len(word) > len(ending) + 1 and word.endswith(ending):
            return word[: -len(ending)]
    return word


def keyword_tokens(value: str) -> tuple[str, ...]:
    """Tokenize a short keyword phrase for order-insensitive comparison."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN.findall(clean_text(value)):
        word = _trim_particle(raw)
        norm = normalize_keyword(word)
        if not norm or _COUNTER.match(word) or _LEADING_YEAR.match(word):
            continue
        if is_low_quality_keyword(word) and norm not in _WEATHER_CLUSTER:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        tokens.append(norm)
    return tuple(tokens)


def token_set_signature(tokens: Sequence[str]) -> str:
    return "|".join(sorted(set(tokens)))


def _cluster_basis(tokens: Sequence[str]) -> tuple[str, ...]:
    compact = set(tokens)
    if compact & _WEATHER_CLUSTER:
        return ("summer_extreme_weather",)
    if "스파이더맨" in compact:
        return ("spider_man",)
    if "워터밤" in compact:
        return ("waterbomb",)
    if "한강" in compact and ({"수영장", "야외수영장"} & compact):
        return ("hangang_pool",)
    if "올리브영" in compact:
        return ("oliveyoung",)
    if "프로야구" in compact and "올스타전" in compact:
        return ("kbo_all_star",)

    basis = tuple(token for token in tokens if token not in _ENTITY_DESCRIPTORS)
    if basis:
        return basis
    return tuple(tokens)


def keyword_signature(value: str) -> KeywordSignature:
    normalized = normalize_keyword(value)
    tokens = keyword_tokens(value)
    signature = token_set_signature(tokens)
    basis = _cluster_basis(tokens)
    cluster_source = "|".join(basis) or signature or normalized
    digest = sha1(cluster_source.encode("utf-8")).hexdigest()[:10]
    return KeywordSignature(
        normalized=normalized,
        tokens=tokens,
        token_set_signature=signature,
        cluster_id=f"cluster_{digest}",
    )


def jaccard_similarity(a: Iterable[str], b: Iterable[str]) -> float:
    left = set(a)
    right = set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def are_similar(a: KeywordSignature, b: KeywordSignature) -> bool:
    if a.normalized and a.normalized == b.normalized:
        return True
    if a.token_set_signature and a.token_set_signature == b.token_set_signature:
        return True
    if a.cluster_id and a.cluster_id == b.cluster_id:
        return True
    similarity = jaccard_similarity(a.tokens, b.tokens)
    if similarity >= 0.75:
        return True
    # Short phrases like "장마" and "장마 폭염" need stricter handling through
    # cluster ids above; below 0.75 they can stay separate unless the entity matched.
    return False


def naturalness_score(keyword: str, signature: KeywordSignature) -> tuple[int, int, int]:
    """Higher is a better display representative within the same cluster."""
    text = keyword.strip()
    has_connector = int(any(connector in text for connector in ("와 ", "과 ", "·", "및")))
    token_count = len(signature.tokens)
    # Prefer specific phrases, but avoid long news-title fragments.
    length_penalty = -abs(len(text) - 8)
    return (has_connector, token_count, length_penalty)
