"""자연 사진 계획 검증·선정 — 저장 호환 카드 모델을 코드가 다시 강제한다.

모델 프롬프트가 이미 같은 규칙을 요구하지만, 규격이 "모델이 지켜 주기를 바라는 것"에
머물면 흔들릴 때마다 예산이 깨진다. 여기서 강제하는 것:

- 썸네일 정확히 1장(없으면 계획 전체를 무효로 — 폴백 경로가 이어받는다)
- necessityScore 80점 미만 제외
- articleClaim이 원고에 실제로 있는지 대조(원고에 없는 주장은 자동 불합격)
- 같은 섹션에 표·그래프가 이미 있으면 사진 카드 제외(표가 사진보다 잘 설명하는 경우)
- 비슷한 장면·같은 섹션 중복 제거(점수 높은 것 하나만)
- 고유 대상(캐릭터·실제 인물) 카드는 그 대상이 실제로 주요 피사체인지 확인
- 전체 예산: 썸네일 1 + 본문(사진+표·그래프+첨부 이미지) 0~5 = 1~6장.
  초과 시 점수 낮은 것부터 제외. 최소 장수는 없다.
"""

import re
from dataclasses import dataclass, field

from app.shared import NAMED_SUBJECT_KINDS, CardBrief, PlannedVisual, VisualCardPlan

MIN_TOTAL_IMAGES = 1
MAX_TOTAL_IMAGES = 6
MIN_NECESSITY_SCORE = 80.0

_WORDS = re.compile(r"[0-9A-Za-z가-힣]+")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORDS.findall(text or "")}


def claim_in_article(claim: str, article: str) -> bool:
    """articleClaim이 원고에 실제로 있는 문장인지.

    공백·문장부호 차이는 눈감아 준다(정규화 부분 문자열). 그래도 안 맞으면 단어 겹침으로
    한 번 더 본다 — 모델이 조사 하나를 다듬어 옮기는 일이 잦아, 문자 그대로만 요구하면
    멀쩡한 카드가 다 떨어진다. 단어의 70% 이상이 원고에 없으면 지어낸 주장으로 본다.
    """
    normalized_claim = "".join(_WORDS.findall(claim or "")).lower()
    normalized_article = "".join(_WORDS.findall(article or "")).lower()
    if not normalized_claim:
        return False
    if normalized_claim in normalized_article:
        return True
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return False
    article_tokens = _tokens(article)
    overlap = len(claim_tokens & article_tokens) / len(claim_tokens)
    return overlap >= 0.7


# 고유 대상 이름에서 걸러 낼 수식어. "Spider-Man (the character)"처럼 붙는 일반 명사만
# 떨어뜨리는 최소한의 목록이며, 여기에 없는 이름은 그대로 대조에 쓴다 — 특정 영어 표현을
# 하드코딩해 판정하지 않는다(그 방식은 새 소재마다 목록을 늘려야 한다).
_IDENTITY_FILLER = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "character",
    "person",
    "actor",
    "player",
    "singer",
    "hero",
    "superhero",
}


def _significant_tokens(text: str) -> list[str]:
    return [
        token
        for token in (t.lower() for t in _WORDS.findall(text or ""))
        if len(token) >= 2 and token not in _IDENTITY_FILLER
    ]


# 사람을 '종류'로 바꿔 적은 표현. 이름이 함께 있으면 문제가 아니지만, 이름 없이 이것만
# 남으면 그 카드는 이름 없는 일반인 사진이 된다. 한국어·영어를 모두 본다.
_GENERIC_PERSON_TERMS = (
    "woman",
    "man",
    "girl",
    "boy",
    "lady",
    "model",
    "person",
    "people",
    "idol",
    "singer",
    "vocalist",
    "performer",
    "artist",
    "dancer",
    "celebrity",
    "student",
    "player",
    "footballer",
    "athlete",
    "actor",
    "actress",
    "trainer",
    "여성",
    "남성",
    "여자",
    "남자",
    "인물",
    "모델",
    "아이돌",
    "가수",
    "보컬",
    "댄서",
    "배우",
    "선수",
    "축구선수",
    "학생",
    "연예인",
)


def _mentions_generic_person(text: str) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in _GENERIC_PERSON_TERMS)


def named_subject_problem(card: CardBrief) -> str | None:
    """고유 대상 카드의 구조 검증. 문제가 있으면 사유 한 줄, 없으면 None.

    보는 것은 넷이다 — 고유 대상인데 (1) 이름이 없다 (2) 보여 주지 않아도 된다고 표시했다
    (3) 그 이름이 주요 피사체에 없다 (4) 이름 대신 사람의 종류만 적혀 있다.

    (3)이 핵심이다: mainSubject가 'a young woman wearing headphones', 'a city skyline'처럼
    대상 주변이나 종류만 가리키면 이름이 어디에도 없으므로 걸린다. 특정 문구 몇 개를
    대조하는 것이 아니라 **정확한 이름이 살아 있는가**를 본다. (4)는 같은 판정의 사유를
    구체적으로 남기기 위한 것이다 — 왜 떨어졌는지가 로그에 남아야 고칠 수 있다.
    """
    if card.subject_kind not in NAMED_SUBJECT_KINDS:
        return None
    identity = (card.subject_identity or "").strip()
    if not identity:
        return f"{card.subject_kind}인데 subjectIdentity가 비어 있음"
    if not card.must_show_subject:
        return f"{card.subject_kind}인데 mustShowSubject가 false"
    main_subject = card.scene.main_subject
    identity_tokens = set(_significant_tokens(identity))
    subject_tokens = set(_significant_tokens(main_subject))
    if identity_tokens and not (identity_tokens & subject_tokens):
        if _mentions_generic_person(main_subject):
            return (
                f"고유 대상 '{identity}' 대신 일반 인물 표현만 계획됨"
                f"(scene.mainSubject='{main_subject}')"
            )
        return (
            f"고유 대상 '{identity}'이(가) scene.mainSubject"
            f"('{main_subject}')에 없음 — 주변 장면만 계획됨"
        )
    return None


@dataclass(frozen=True)
class NamedSubject:
    """이 사진이 반드시 보여 줘야 하는 고유 대상. 카드 계획이 실패하거나 계획 썸네일
    생성이 실패해 폴백으로 내려갈 때도 정체성이 따라가게 하는 운반 값이다.

    reference_ids는 인물 확인에 쓸 참고 이미지의 id다(data URL이 아니다 — 실제 이미지는
    서비스가 생성 직전에 붙인다)."""

    kind: str
    identity: str
    must_show: bool = True
    confidence: float = 0.0
    reference_ids: tuple[str, ...] = ()


def named_subject_of(card: CardBrief | None) -> NamedSubject | None:
    """카드가 고유 대상(캐릭터·실제 인물)을 가리키면 그 정보, 아니면 None."""
    if card is None or card.subject_kind not in NAMED_SUBJECT_KINDS:
        return None
    identity = (card.subject_identity or "").strip()
    if not identity:
        return None
    return NamedSubject(
        kind=card.subject_kind,
        identity=identity,
        must_show=True,
        confidence=card.identity_confidence,
        reference_ids=tuple(card.reference_person_images),
    )


def plan_named_subject(plan: VisualCardPlan | None) -> NamedSubject | None:
    """계획 전체가 가리키는 고유 대상. 썸네일이 먼저고, 없으면 그렇게 표시한 첫 카드다.

    계획이 규격에 걸려 통째로 버려질 때도 '이 글은 스파이더맨 글'이라는 사실만은 폴백
    경로로 넘긴다 — 계획을 버렸다고 소재의 정체성까지 버리지 않는다.
    """
    if plan is None:
        return None
    cards = sorted(plan.cards, key=lambda c: 0 if c.card_type == "THUMBNAIL" else 1)
    for card in cards:
        named = named_subject_of(card)
        if named is not None:
            return named
    return None


def _scene_key(card: CardBrief) -> str:
    """장면 중복 판정 키.

    피사체와 장소만 같아도 다른 행동·구도인 실제 사진은 충분히 달라질 수 있다. 행동과
    카메라 거리·각도까지 함께 봐, 필요한 사진을 과하게 중복 처리하지 않는다.
    """
    fields = (
        card.scene.main_subject,
        card.scene.action,
        card.scene.setting,
        card.scene.camera_angle,
        card.scene.camera_distance,
    )
    return "|".join("".join(_WORDS.findall(value or "")).lower() for value in fields)


def section_number(section_id: str | None) -> int | None:
    match = re.fullmatch(r"section-(\d+)", (section_id or "").strip())
    return int(match.group(1)) if match else None


@dataclass
class SelectedCards:
    """예산을 통과한 최종 구성. 순번(1부터)은 배치 순서 — 썸네일 → 첨부 이미지 →
    본문(사진 카드·표·그래프를 섹션 순서로)."""

    thumbnail: CardBrief
    body_cards: list[CardBrief] = field(default_factory=list)
    visuals: list[PlannedVisual] = field(default_factory=list)
    reference_count: int = 0
    # 각 항목의 순번. 카드/시각자료 순번은 id로 찾는다.
    card_numbers: dict[str, int] = field(default_factory=dict)
    visual_numbers: dict[str, int] = field(default_factory=dict)
    reference_numbers: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return 1 + self.reference_count + len(self.body_cards) + len(self.visuals)


def select_cards(
    plan: VisualCardPlan,
    article_body: str,
    renderable_visuals: list[PlannedVisual],
    reference_count: int,
    rejections: list[str] | None = None,
    max_total: int = MAX_TOTAL_IMAGES,
) -> SelectedCards | None:
    """계획에서 규격을 통과한 카드만 추려 순번까지 매긴다. 썸네일이 없으면 None —
    폴백(기존 방식)이 이어받는다. rejections에 제외 사유를 남긴다(로그용).

    max_total은 썸네일을 포함한 **사진** 최대 장수다 — 글 길이가 정한다(짧게 2~3장·중간
    3~5장, prompts.ARTICLE_LENGTH_IMAGE_RANGES). 계획이 그보다 많이 세워 와도 여기서 잘린다.
    표·그래프는 이 총량에 넣지 않는다(2026-08-03 사용자 결정 — AI 판단·근거 원칙만 적용).
    """
    notes = rejections if rejections is not None else []
    max_total = max(MIN_TOTAL_IMAGES, min(max_total, MAX_TOTAL_IMAGES))

    thumbnail = next(
        (card for card in plan.cards if card.card_type == "THUMBNAIL"), None
    )
    if thumbnail is None:
        notes.append("계획에 THUMBNAIL 카드가 없음")
        return None

    # 대표 썸네일이 고유 대상을 놓치면 글의 얼굴이 '주제와 관련된 일반적인 사진'이 된다.
    # 여기서는 카드 한 장을 버리는 것으로 고칠 수 없으므로 계획 전체를 폴백에 넘긴다
    # (폴백 경로도 고유 대상 정보를 그대로 들고 간다 — service._with_post_images).
    thumbnail_problem = named_subject_problem(thumbnail)
    if thumbnail_problem is not None:
        notes.append(f"{thumbnail.card_id}(썸네일): {thumbnail_problem}")
        return None

    visual_sections = {
        number
        for number in (section_number(v.section_id) for v in renderable_visuals)
        if number is not None
    }

    candidates: list[CardBrief] = []
    seen_scenes: set[str] = set()
    seen_sections: set[int] = set()
    for card in sorted(
        (c for c in plan.cards if c.card_type == "SECTION_CARD"),
        key=lambda c: -c.necessity_score,
    ):
        if card.necessity_score < MIN_NECESSITY_SCORE:
            notes.append(f"{card.card_id}: 필요성 {card.necessity_score:.0f}점 (<80)")
            continue
        if not claim_in_article(card.article_claim, article_body):
            notes.append(f"{card.card_id}: articleClaim이 원고에 없음")
            continue
        problem = named_subject_problem(card)
        if problem is not None:
            notes.append(f"{card.card_id}: {problem}")
            continue
        section = section_number(card.section_id)
        if section is not None and section in visual_sections:
            notes.append(f"{card.card_id}: 같은 섹션에 표·그래프가 이미 있음")
            continue
        if section is not None and section in seen_sections:
            notes.append(f"{card.card_id}: 같은 섹션의 카드가 이미 선정됨")
            continue
        key = _scene_key(card)
        if key in seen_scenes:
            notes.append(f"{card.card_id}: 비슷한 장면의 카드가 이미 선정됨")
            continue
        seen_scenes.add(key)
        if section is not None:
            seen_sections.add(section)
        candidates.append(card)

    # 본문 사진 예산: 첨부 이미지가 먼저 자리를 차지하고, 남는 자리를 점수순 카드가
    # 채운다. 표·그래프는 이 예산과 무관하다(2026-08-03 사용자 결정) — 사진 장수는
    # 사용자 규격이지만, 근거를 통과한 표·그래프를 사진 자리 때문에 버리지 않는다.
    kept_visuals = list(renderable_visuals)
    body_budget = max_total - 1  # 썸네일 몫을 뺀 본문 사진 자리
    kept_references = min(reference_count, body_budget)
    if kept_references < reference_count:
        notes.append(f"첨부 이미지 {reference_count - kept_references}개: 전체 예산 초과")
    remaining = body_budget - kept_references
    kept_cards = candidates[:remaining]
    for dropped in candidates[remaining:]:
        notes.append(f"{dropped.card_id}: 전체 예산({max_total}장) 초과 — 점수 낮은 순 제외")

    selected = SelectedCards(
        thumbnail=thumbnail,
        body_cards=kept_cards,
        visuals=kept_visuals,
        reference_count=kept_references,
    )
    assign_numbers(selected)
    return selected


def assign_numbers(selected: SelectedCards) -> None:
    """순번(1부터)을 배치 순서대로 다시 매긴다: 썸네일 1 → 첨부 이미지 → 본문(섹션
    순서, 같은 섹션이면 표·그래프 먼저, 섹션 없는 항목은 마지막).

    전체 장수가 확정된 뒤에만 부른다(§4). 생성 실패로 카드가 빠지면 서비스가 생존
    카드로 다시 부른다 — 순번이 건너뛴 카드(2/5 다음 4/5)는 자동 불합격이다."""
    selected.card_numbers = {}
    selected.visual_numbers = {}
    selected.reference_numbers = []

    number = 1
    selected.card_numbers[selected.thumbnail.card_id] = number
    number += 1
    for _ in range(selected.reference_count):
        selected.reference_numbers.append(number)
        number += 1

    def order_key(kind: str, section_id: str | None) -> tuple[int, int]:
        section = section_number(section_id)
        return (section if section is not None else 999, 0 if kind == "visual" else 1)

    body_items: list[tuple[str, str | None, str]] = [
        ("visual", visual.section_id, visual.visual_id) for visual in selected.visuals
    ] + [("card", card.section_id, card.card_id) for card in selected.body_cards]
    for kind, section_id, item_id in sorted(
        body_items, key=lambda item: order_key(item[0], item[1])
    ):
        if kind == "visual":
            selected.visual_numbers[item_id] = number
        else:
            selected.card_numbers[item_id] = number
        number += 1
