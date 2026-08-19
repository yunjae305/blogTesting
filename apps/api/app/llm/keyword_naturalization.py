"""원본 검색 키워드와 '글에서 쓸 표현'을 분리한다.

사용자가 트렌드 패널에서 고르는 것은 **검색어 조합**이다("창섭 전과자", "백지헌
프로미스나인"). 검색창에는 그렇게 넣지만, 문장에서는 그렇게 쓰지 않는다 — 두 고유명사를
띄어쓰기로만 이어 붙인 문자열에 조사를 달면("창섭 전과자는") 한국어 문장이 아니다.

지금까지는 그 조합을 제목에 **그대로** 넣으라는 규칙이 M2에 있었고(blend_rules), 그 제목이
확정되면 SEO Primary도 제목에서 뽑히므로(parsing.keyword_inside_title) 검증까지 그 비문을
요구했다. 프롬프트 한 줄을 고쳐 막을 수 있는 문제가 아니라, 파이프라인이 두 값을 같은
것으로 취급한 것이 원인이다.

여기 있는 것은 그 분리를 위한 판단 함수들이다. 전부 순수 함수이고 외부 호출이 없다.

- ``is_single_token_keyword``  — 분해할 이유가 없는 키워드인가('아이폰17').
- ``raw_keyword_misuse``       — 검색어 조합을 명사처럼 쓴 자리를 찾는다('창섭 전과자는').
- ``keyword_meaning_covered``  — 연속 문자열이 아니어도 검색 의도가 담겼는가.

특정 프로그램·인물 이름은 어디에도 없다. 판단은 토큰 구조와 엔티티 관계로만 한다.
"""

from __future__ import annotations

import re

# 비교용 정규화. 조사·띄어쓰기·문장부호 차이만 다른 표현은 같은 것으로 본다
# (quality.normalize_for_match·parsing._seo_norm과 같은 규칙).
_NORM = re.compile(r"[^0-9a-z가-힣]")
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

# 검색어 조합 뒤에 붙으면 '하나의 명사처럼 썼다'는 신호가 되는 조사.
RAW_KEYWORD_MISUSE_PARTICLES = (
    "는",
    "은",
    "이",
    "가",
    "를",
    "을",
    "의",
    "도",
    "에",
    "에서",
    "와",
    "과",
    "로",
    "으로",
)

# 검색어 조합을 명사구의 머리로 쓴 형태('… 편', '… 프로그램'). 조사와 달리 띄어 쓰는 것이
# 보통이라, 보고할 때 사이에 공백을 넣는다.
RAW_KEYWORD_MISUSE_SUFFIX_NOUNS = (
    "편",
    "프로그램",
    "영상",
    "채널",
    "시리즈",
    "출연",
)

RAW_KEYWORD_MISUSE_SUFFIXES = (
    *RAW_KEYWORD_MISUSE_PARTICLES,
    *RAW_KEYWORD_MISUSE_SUFFIX_NOUNS,
)

# 한 문장으로 끊는 자리. 제목처럼 종결부호가 없는 짧은 문자열은 통째로 한 문장이다.
# 종결어미('~다')로만 끊지 않는다 — '한다 하더라도'처럼 문장 중간의 '다'를 끊으면 멀쩡한
# 문장이 둘로 갈려 오탐이 난다.
_SENTENCE_SPLIT = re.compile(r"[.!?。\n]+")

# 의미 판정에서 무시하는 짧은 토큰. 한 글자는 어디에나 있어 근거가 되지 못한다.
_MIN_TOKEN_CHARS = 2


def normalize(text: str) -> str:
    """조사·띄어쓰기·대소문자를 걷어낸 비교용 문자열."""
    return _NORM.sub("", (text or "").lower())


def keyword_tokens(keyword: str) -> list[str]:
    """검색어를 의미 토큰으로 자른다. 한 글자 토큰은 근거가 못 되므로 버린다."""
    tokens = [t.lower() for t in _TOKEN.findall(keyword or "")]
    return [t for t in tokens if len(t) >= _MIN_TOKEN_CHARS]


def is_single_token_keyword(keyword: str) -> bool:
    """분해할 이유가 없는 키워드인가.

    '아이폰17'처럼 띄어쓰기 없이 하나의 고유명사로 읽히는 검색어는 예전처럼 문장에 그대로
    쓸 수 있다. 이번 변경이 **모든** 키워드를 쪼개면 멀쩡한 소재까지 어색해진다.
    """
    return len(keyword_tokens(keyword)) <= 1


def is_entity_juxtaposition(keyword: str, entity_names: list[str]) -> bool:
    """검색어가 서로 다른 고유명사 둘 이상을 띄어쓰기로만 이어 붙인 조합인가.

    이 판정이 필요한 이유: 토큰이 둘 이상이라고 다 문제가 아니다. '전과자 학과 체험'은
    조사를 붙여도 자연스러운 명사구다. 문제가 되는 것은 **서로 독립적인 고유명사**를
    나란히 둔 검색어다 — 사람 이름과 프로그램 이름을 붙여 놓고 조사를 달면('창섭 전과자는')
    한국어 문장이 아니다.

    ``entity_names``는 검색으로 확인된 이름들(정식 프로그램명·출연자 이름 등)이고,
    특정 이름이 코드에 박히지 않는다. 확인된 이름이 없으면 판정할 근거가 없으므로 False다.
    """
    tokens = keyword_tokens(keyword)
    if len(tokens) < 2:
        return False
    matched: set[str] = set()
    for token in tokens:
        token_norm = normalize(token)
        if not token_norm:
            continue
        for name in entity_names:
            name_norm = normalize(name)
            if not name_norm:
                continue
            # 축약명('창섭')과 공식 이름('이창섭')은 서로를 포함한다 — 어느 방향이든 같은 대상.
            if token_norm in name_norm or name_norm in token_norm:
                matched.add(name_norm)
                break
    return len(matched) >= 2


def raw_keyword_misuse(
    text: str, keyword: str, suffixes: tuple[str, ...] = RAW_KEYWORD_MISUSE_SUFFIXES
) -> list[str]:
    """검색어 조합을 하나의 명사처럼 쓴 자리들. 없으면 빈 목록.

    토큰이 하나뿐인 검색어('아이폰17')는 정상적인 고유명사라 검사하지 않는다 — 조사가
    붙는 것이 오히려 자연스럽다. 두 개 이상의 토큰이 띄어쓰기로만 이어진 조합만 본다.
    호출부는 그 조합이 실제로 서로 다른 고유명사의 나열인지(``is_entity_juxtaposition``)
    먼저 확인해야 한다 — 자연스러운 명사구까지 금지하면 안 된다.

    띄어쓰기 차이는 무시한다('창섭 전과자는'과 '창섭전과자는'은 같은 오용이다).
    """
    tokens = keyword_tokens(keyword)
    if len(tokens) < 2:
        return []
    keyword_norm = normalize(keyword)
    if not keyword_norm:
        return []

    # 정규화한 본문에서 찾고, 걸린 자리는 원문 대신 '검색어+꼬리' 형태로 돌려준다 —
    # 재생성 지시에 그대로 실려 무엇이 문제인지 모델이 알아볼 수 있어야 한다.
    text_norm = normalize(text)
    found: list[str] = []
    for suffix in suffixes:
        pattern = keyword_norm + normalize(suffix)
        if not pattern or pattern not in text_norm:
            continue
        # 조사는 붙여 쓰고, 접미 명사는 띄어 쓴 형태로 보고한다 — 재생성 지시에 그대로
        # 실리므로 모델이 무엇이 문제인지 알아볼 수 있어야 한다.
        separator = " " if suffix in RAW_KEYWORD_MISUSE_SUFFIX_NOUNS else ""
        found.append(f"{keyword}{separator}{suffix}")
    return found


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text or "")]
    return [part for part in parts if part]


def _token_covered(token: str, haystack_norm: str, aliases: dict[str, tuple[str, ...]]) -> bool:
    """토큰 하나가 이 문장에 담겼는가. 별칭(공식 이름 ↔ 축약명)도 인정한다.

    '창섭'은 '이창섭' 안에 들어 있으므로 부분 문자열 검사만으로 통과한다. 반대 방향
    ('이창섭'을 찾는데 본문에는 '창섭')은 별칭 표에서 온다.
    """
    if normalize(token) in haystack_norm:
        return True
    return any(
        normalize(alias) and normalize(alias) in haystack_norm
        for alias in aliases.get(token.lower(), ())
    )


def keyword_meaning_covered(
    text: str,
    keyword: str,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """검색 의도가 이 글(제목·문단)에 담겼는가 — 연속 문자열이 아니어도 인정한다.

    '이창섭이 대학 수업을 직접 듣는 유튜브 웹예능 전과자'에는 '창섭 전과자'라는 연속
    문자열이 없지만, 검색 의도는 정확히 반영돼 있다. 반대로 토큰이 서로 다른 문단에
    흩어져 있는 글은 그 검색어의 글이 아니다 — 그래서 **한 문장(또는 제목) 안에서**
    모든 핵심 토큰이 확인될 때만 통과시킨다.

    토큰이 하나뿐인 검색어는 예전과 같다: 그 표현이 실제로 있어야 통과한다.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    aliases = aliases or {}

    text_norm = normalize(text)
    keyword_norm = normalize(keyword)
    if not keyword_norm or not text_norm:
        return False
    # 그대로 들어 있으면 더 볼 것이 없다(기존 동작).
    if keyword_norm in text_norm:
        return True

    tokens = keyword_tokens(keyword)
    if len(tokens) < 2:
        # 단일 토큰은 별칭까지만 열어 둔다. 여기서 더 느슨해지면 아무 글이나 통과한다.
        return _token_covered(keyword, text_norm, aliases)

    for sentence in _sentences(text):
        sentence_norm = normalize(sentence)
        if not sentence_norm:
            continue
        if all(_token_covered(token, sentence_norm, aliases) for token in tokens):
            return True
    return False


def primary_raw_keyword(draft_input) -> str:
    """이 글의 원본 검색 키워드 하나. 없으면 빈 문자열.

    사용자가 M2에서 고른 것만 본다. 의도 키워드(모델이 만든 검색어)를 여기에 섞지 않는다 —
    '사용자가 그대로 복사하면 안 되는 문자열'과 '모델이 제안한 검색어'는 다른 것이고,
    섞으면 멀쩡한 명사구까지 금지 대상이 된다.
    """
    for keyword in getattr(draft_input, "raw_keywords", None) or []:
        text = (keyword or "").strip()
        if text:
            return text
    return ""


def entity_aliases(names: list[str]) -> dict[str, tuple[str, ...]]:
    """축약명 ↔ 공식 이름의 별칭 표를 이름 목록에서 만든다.

    '이창섭'을 알고 있으면 '창섭'도 같은 사람이다. 성을 뗀 형태를 별칭으로 넣어 두면
    검색어가 축약명으로 왔을 때(그리고 그 반대일 때) 양쪽 모두 인정된다. 한국 이름의
    성이 한 글자라는 관례만 쓰고, 특정 인물 이름은 코드에 두지 않는다.
    """
    table: dict[str, tuple[str, ...]] = {}
    for raw in names:
        name = (raw or "").strip()
        if len(name) < 3 or not re.fullmatch(r"[가-힣]+", name):
            continue
        short = name[1:]
        table.setdefault(short.lower(), ())
        table[short.lower()] = tuple({*table[short.lower()], name})
        table.setdefault(name.lower(), ())
        table[name.lower()] = tuple({*table[name.lower()], short})
    return table
