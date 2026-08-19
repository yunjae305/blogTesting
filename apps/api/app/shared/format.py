"""여러 모듈이 공유하는 작은 포맷 헬퍼.

같은 구현이 서비스마다 복사돼 있던 것을 한곳으로 모은 것이다.
"""

from datetime import datetime, timezone


def now_iso() -> str:
    """UTC 현재 시각을 밀리초까지, ``Z`` 접미사로 표기한 ISO 문자열."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def escape_html(value: str) -> str:
    """HTML 특수문자를 엔티티로 바꾼다."""
    replacements = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
    return "".join(replacements.get(char, char) for char in value)


#: 받침 없는 소리로 끝나는 로마자. 조사를 붙일 때 한글로 읽은 마지막 소리를 본다 —
#: "AIONA를"이 맞고 "AIONA을"은 틀리다.
_NO_FINAL_CONSONANT_LETTERS = frozenset("aeiouy")
#: 로마자로 적히지만 받침으로 읽히는 끝소리. "Blog-it은", "AI는"처럼 갈린다.
_FINAL_CONSONANT_LETTERS = frozenset("lmnr")


def has_final_consonant(word: str) -> bool | None:
    """이 낱말이 받침으로 끝나는가. 판단할 수 없으면 ``None``.

    한글은 유니코드로 계산된다(가~힣은 초성·중성·종성이 규칙적으로 배열돼 있어, 종성
    자리가 0이면 받침이 없다). 숫자·로마자는 **읽는 소리**로 갈리므로 흔한 것만 본다.
    """
    text = "".join(char for char in (word or "").strip() if not char.isspace())
    if not text:
        return None
    last = text[-1]
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28 != 0
    if last.isdigit():
        # 0(영)·1(일)·3(삼)·6(육)·7(칠)·8(팔)은 받침으로 끝난다.
        return last in "013678"
    lowered = last.lower()
    if lowered in _FINAL_CONSONANT_LETTERS:
        return True
    if lowered in _NO_FINAL_CONSONANT_LETTERS:
        return False
    return None


#: 조사를 고를 때 무시하는 끝문자. 프롬프트·안내 문장은 이름을 따옴표나 괄호로 감싸
#: 쓰는 일이 많은데("'빼빼로'과(와)"), 그 부호가 마지막 소리는 아니다.
_TRAILING_MARKS = "\"'`)]}»›〉》」』.…,"


def with_particle(word: str, after_consonant: str, after_vowel: str) -> str:
    """낱말에 조사를 붙인다 — "자료 조사를", "AIONA를", "빼빼로와".

    문장을 만들어 사람에게 보여 주는 자리(판정 이유·프롬프트 지시문)에서 쓴다.
    "자료 조사을(를)"처럼 두 형태를 나란히 적으면, 그 문장을 읽는 것이 사람이든
    모델이든 어색한 글이 된다 — 모델은 그 표기를 그대로 원고에 옮기기도 한다.

    판단할 수 없는 낱말(한자·기호로 끝나는 이름)에서만 예전처럼 두 형태를 함께 적는다.
    """
    final = has_final_consonant((word or "").rstrip(_TRAILING_MARKS))
    if final is None:
        return f"{word}{after_consonant}({after_vowel})"
    return f"{word}{after_consonant if final else after_vowel}"
