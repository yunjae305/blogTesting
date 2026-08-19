"""문장 다듬기가 제안한 교정을 완성 원고에 반영한다(M4 5단계).

이 모듈은 **모델을 부르지 않는다**. 다듬기 모델이 돌려준 before → after를 실제 원고에
넣기 전에 한 건씩 검사하고, 통과한 것만 반영하는 순수 함수만 있다.

왜 검사가 필요한가. 이 단계가 하는 일은 "문장을 자연스럽게 만들라"이고, 그것은 모델이
가장 잘하는 동시에 가장 위험한 지시다 — 가장 자연스러운 블로그 문장은 겪어 본 사람이 쓴
문장이고, 가장 매끄러운 설명은 구체적인 숫자가 붙은 설명이다. 아무 제약 없이 맡기면
다듬기가 조용히 사실을 만들어 낸다. 그래서 규칙은 프롬프트에만 적지 않고 여기서
**원고와 대조해 강제**한다: 숫자가 달라지면 버리고, 없던 경험이 생기면 버리고, 검색
키워드가 사라지면 버리고, 구조 표식을 물고 있으면 버린다.

거절한 교정도 결과에 남긴다(PolishEdit.rejected_rule). 무엇을 막았는지가 무엇을 고쳤는지
만큼 중요한 자리이기 때문이다 — 남기지 않으면 규칙이 빡빡한지 느슨한지 알 방법이 없다.
"""

import logging
import re

from app.llm.prompts import EXPERIENCE_CLAIM_PHRASES
from app.shared import FinalPost, PolishEdit

from .final_review import apply_sentence_replacement
from .quality import normalize_for_match

logger = logging.getLogger(__name__)

# 거절 사유. 결과 문서에 그대로 저장되므로 짧고 고정된 문자열이어야 한다.
REJECT_NOT_FOUND = "원고에 없음"
REJECT_NUMBER_CHANGED = "수치 변경"
REJECT_FAKE_EXPERIENCE = "지어낸 경험"
REJECT_KEYWORD_DROPPED = "키워드 삭제"
REJECT_STRUCTURE = "구조 표식"
REJECT_HEADING = "소제목"
REJECT_TOO_LONG = "문장 늘림"
REJECT_TONE_SHIFT = "문체 변경"

# 잡으면 안 되는 줄. 이미지 자리 표식·표·목록 기호·HTML·마크다운 이미지가 들어간 문장을
# 통째로 바꾸면 그림이 사라지거나 표가 깨진다. 다듬기가 할 일은 문장이지 레이아웃이 아니다.
_STRUCTURE_MARKS = (
    "[[IMAGE",
    "[[VISUAL",
    "[[STICKER",
    "![",
    "](",
    "data:",
    "<img",
    "<figure",
    "<table",
    "<p>",
    "</",
    "|",
)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")

# 문장 속 숫자. 천 단위 쉼표와 소수점을 하나로 묶어 "1,200"과 "1200"을 같은 값으로 본다.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# 겪지 않으면 쓸 수 없는 말. 프롬프트가 이미 목록으로 금지하지만(EXPERIENCE_CLAIM_PHRASES),
# 모델은 목록에 없는 변형을 만들어 낸다("제가 방문했을 때", "영화를 보고 나서") — 그래서
# 문자열 목록이 아니라 형태로 잡는다.
_EXPERIENCE_VERBS = "사용|이용|구매|주문|방문|체험|시청|관람|측정"
_FABRICATED_EXPERIENCE = tuple(
    re.compile(pattern)
    for pattern in (
        # 1인칭 주어 + 겪었다는 동사("제가 방문했을 때", "저는 구매해서")
        rf"(?:제가|저는|나는|내가)\s*.{{0,20}}(?:{_EXPERIENCE_VERBS})(?:해|했|한)",
        rf"(?:제가|저는|나는|내가)\s*.{{0,20}}(?:먹어|써|입어|봐)\s*(?:봤|본|보니)",
        # 주어 없이 겪었다고 말하는 형태("직접 확인해 보니", "실제로 구매해서").
        # 독자에게 권하는 말("직접 확인해 보세요")은 화자의 경험이 아니므로 뒤를 보고 뺀다.
        rf"(?:직접|실제로)\s*.{{0,12}}(?:{_EXPERIENCE_VERBS}|확인|경험)(?:해|했)"
        r"(?!\s*(?:보세요|보시|볼|봐야|보는))",
        rf"(?:{_EXPERIENCE_VERBS})해\s*보니",
        r"(?:써|가|먹어|입어|봐)\s*봤(?:는데|더니|습니다|어요|고)",
        r"(?:써|가|먹어|입어|봐|보고)\s*(?:본|난)\s*(?:결과|후기|느낌|소감)",
        r"현장에서\s*(?:직접\s*)?(?:확인|경험)",
        # 보고 나서 느낀 점 — 영상·공연은 '써 봤다'가 아니라 '봤다'로 조작된다.
        r"(?:영화|드라마|공연|경기|콘서트)를?\s*보고\s*(?:나서|난\s*뒤)",
    )
)

# after가 before보다 이만큼 길어지면 다듬기가 아니라 덧붙이기다. 표현을 바꾸면서 조사·
# 연결어가 늘어나는 정도는 넉넉히 허용하되, 문장 하나를 두 문장으로 불리는 것은 막는다.
_LENGTH_SLACK_RATIO = 1.6
_LENGTH_SLACK_CHARS = 30

# 종결 문체. 다듬기가 '~습니다' 문장을 '~요'로 바꾸면 **손댄 문장만** 말투가 가벼워져,
# 한 글이 앞뒤로 다른 사람이 쓴 것처럼 읽힌다(2026-08-07 사용자 신고: "처음에는 ~다로
# 끝나는데 뒤로 갈수록 ~요처럼 가볍게 된다"). 프롬프트로 금지하는 것과 별개로, 여기서
# 문장의 종결 문체를 분류해 원고의 지배 문체에서 **멀어지는** 교정을 버린다.
# 지배 문체 쪽으로 맞추는 교정(어긋난 한 문장을 고치는 것)은 다듬기의 일이므로 허용한다.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TRAILING_NOISE = re.compile(r"[\s.!?…~)\]}'\"”’」』>]+$")


def ending_style(sentence: str) -> str | None:
    """문장의 종결 문체. '~습니다'체는 hamnida, '~요'체는 yo, '~다'체는 da.

    한국어 종결어미로 끝나지 않으면(명사 종결·영어·숫자) None — 판정하지 않는다.
    권유·명령형 '~세요'도 None이다: '~습니다'체 글에서도 "직접 확인해 보세요"는
    자연스러워서, 이것을 '~요'체로 세면 멀쩡한 권유문 교정이 막힌다.
    """
    text = _TRAILING_NOISE.sub("", sentence.strip())
    if not text:
        return None
    if text.endswith(("니다", "니까", "십시오")):
        return "hamnida"
    if text.endswith(("세요", "셔요")):
        return None
    if text.endswith(("요", "죠")):
        return "yo"
    if text.endswith("다"):
        return "da"
    return None


def dominant_ending_style(text: str) -> str | None:
    """원고 전체에서 가장 많이 쓰인 종결 문체. 판정할 문장이 없으면 None."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        # 소제목·목록·표는 문체 표본이 아니다 — 명사형 종결이 대부분이라 어차피 None이
        # 되지만, 목록 항목의 서술형 꼬리가 표본을 흐리는 것도 막는다.
        if not stripped or stripped.startswith(("#", "-", "*", "|", ">")):
            continue
        for sentence in _SENTENCE_SPLIT.split(stripped):
            style = ending_style(sentence)
            if style:
                counts[style] = counts.get(style, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _shifts_ending_style(before: str, after: str, dominant: str | None) -> bool:
    """교정이 종결 문체를 바꿔 원고의 지배 문체에서 멀어지는가.

    after의 각 문장은 (1) before의 문체를 유지하거나 (2) 지배 문체로 맞추는 것만
    허용한다. 그 밖의 문체가 하나라도 나오면 — '~습니다' 원고에 '~요' 문장을 심는
    관찰된 사례가 정확히 이것이다 — 교정을 버린다.
    """
    allowed = {style for style in (ending_style(before), dominant) if style}
    if not allowed:
        return False
    return any(
        style is not None and style not in allowed
        for style in (ending_style(sentence) for sentence in _SENTENCE_SPLIT.split(after))
    )


def _numbers(text: str) -> list[str]:
    """문장에 등장하는 숫자들. 쉼표는 표기 차이일 뿐이라 걷어내고 비교한다."""
    return sorted(match.group().replace(",", "") for match in _NUMBER.finditer(text))


def _has_structure_mark(text: str) -> bool:
    upper = text.upper()
    if "\n" in text:
        # 여러 줄을 한 번에 잡은 것이다. 문단 경계·목록·표가 그 안에 들어 있다.
        return True
    return any(mark.upper() in upper for mark in _STRUCTURE_MARKS)


def _drops_keyword(before: str, after: str, keywords: tuple[str, ...]) -> bool:
    """이 문장이 들고 있던 검색 키워드를 교정이 떨어뜨렸는가.

    띄어쓰기·문장부호를 무시하고 본다 — 'AI 블로그'를 'AI블로그'로 바꾸는 것은 키워드를
    지운 것이 아니다(quality.normalize_for_match와 같은 기준).
    """
    normalized_before = normalize_for_match(before)
    normalized_after = normalize_for_match(after)
    for keyword in keywords:
        normalized = normalize_for_match(keyword)
        if not normalized:
            continue
        if normalized in normalized_before and normalized not in normalized_after:
            return True
    return False


def _fabricates_experience(before: str, after: str) -> bool:
    """교정이 **없던** 체험 서술을 만들어 냈는가.

    before에 이미 있던 표현은 여기서 잡지 않는다 — 그건 다듬기가 지어낸 것이 아니라
    원고가 원래 들고 있던 문장이고, 그 문장을 고치는 것이야말로 이 단계가 할 일이다.
    """
    if any(phrase in after and phrase not in before for phrase in EXPERIENCE_CLAIM_PHRASES):
        return True
    return any(
        pattern.search(after) and not pattern.search(before)
        for pattern in _FABRICATED_EXPERIENCE
    )


def rejection_rule(
    edit: PolishEdit,
    *,
    keywords: tuple[str, ...],
    allow_experience: bool,
    dominant_style: str | None = None,
) -> str | None:
    """이 교정이 어느 규칙에 걸리는가. 걸리지 않으면 None.

    원고를 보지 않고 before/after만으로 판정할 수 있는 것들이다. 원고에서 문장을 찾지
    못하는 경우(REJECT_NOT_FOUND)만 apply_polish가 실제로 넣어 보며 판정한다.
    ``dominant_style``은 원고 전체의 지배 종결 문체(apply_polish가 계산해 넘긴다) —
    없으면 문체 검사가 통째로 빠진다(옛 호출부 호환).
    """
    before, after = edit.before, edit.after

    if _HEADING.match(before) or _HEADING.match(after):
        # 제목·소제목은 SEO와 목차의 뼈대다. 표현이 어색해도 여기서 바꾸지 않는다.
        return REJECT_HEADING
    if _has_structure_mark(before) or _has_structure_mark(after):
        return REJECT_STRUCTURE
    if _numbers(before) != _numbers(after):
        # 새 수치를 넣는 것도, 있던 수치를 지우는 것도 막는다. 이 단계는 표현만 바꾼다 —
        # 숫자가 달라졌다면 그것은 다듬기가 아니라 사실 편집이다(빈 after도 여기 걸린다).
        return REJECT_NUMBER_CHANGED
    if not allow_experience and _fabricates_experience(before, after):
        return REJECT_FAKE_EXPERIENCE
    if _drops_keyword(before, after, keywords):
        return REJECT_KEYWORD_DROPPED
    if after.strip() and _shifts_ending_style(before, after, dominant_style):
        return REJECT_TONE_SHIFT
    if len(after) > len(before) * _LENGTH_SLACK_RATIO + _LENGTH_SLACK_CHARS:
        return REJECT_TOO_LONG
    return None


def apply_polish(
    post: FinalPost,
    edits: list[PolishEdit],
    *,
    keywords: tuple[str, ...] = (),
    allow_experience: bool = False,
) -> tuple[FinalPost, list[PolishEdit]]:
    """교정들을 검사해 통과한 것만 원고에 반영한다.

    반환: (다듬은 원고, 판정을 채운 교정 목록). 목록에는 제안된 교정이 **전부** 들어 있고,
    각 항목의 applied/rejected_rule이 무엇이 반영되고 무엇이 왜 막혔는지를 말한다.

    ``keywords``는 문장에 남아 있어야 하는 SEO 키워드(계획이 없으면 빈 튜플이고, 그때는
    키워드 검사가 통째로 빠진다). ``allow_experience``는 사용자가 실제 경험 자료를 줬는가다.
    """
    updated = post
    judged: list[PolishEdit] = []
    # 원고의 지배 종결 문체. 교정이 이 문체에서 멀어지면(예: '~습니다' 원고에 '~요'
    # 문장을 심으면) 버린다 — 문체 쪽으로 맞추는 교정은 통과한다.
    dominant = dominant_ending_style(post.body or "")

    for edit in edits:
        rule = rejection_rule(
            edit,
            keywords=keywords,
            allow_experience=allow_experience,
            dominant_style=dominant,
        )
        if rule is None:
            applied = apply_sentence_replacement(updated, edit.before, edit.after)
            if applied is None:
                rule = REJECT_NOT_FOUND
            else:
                updated = applied

        if rule is not None:
            logger.info("문장 다듬기 거절(%s) | %s", rule, edit.before[:60])
            judged.append(edit.model_copy(update={"applied": False, "rejected_rule": rule}))
            continue

        judged.append(edit.model_copy(update={"applied": True, "rejected_rule": None}))

    return updated, judged
