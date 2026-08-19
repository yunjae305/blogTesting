"""목적별 시각자료 게이트 — "최대 몇 개까지"가 아니라 "필요할 때만".

## 무엇이 달라졌나

예전 규칙은 상한만 있었다: 코드 렌더링 시각자료 0~4개. 상한만 있으면 모델은 늘 상한
근처를 채운다 — 일상 공유 글에도 인포그래픽이, 수치 없는 트렌드 글에도 표가 붙었다.

여기서는 **기본값이 NONE**이다. 목적마다 어떤 유형이 허용되고 몇 개까지인지 코드가
정하고, 프롬프트가 같은 규칙을 말한다. 시각자료가 0개인 결과는 실패가 아니라 정상적인
최종 결과다.

## 두 겹의 방어

1. 계획 단계 — 프롬프트가 목적별 허용 유형만 제시한다(prompts.visual_gate_rules).
2. 생성 이후 — 여기 ``gate_visuals``가 실제로 잘라 낸다. 프롬프트가 흔들려도 결과는
   정책을 지킨다.

원고 전체를 실패시키지 않는다. 근거가 없는 자료는 **그 자료만** 빼고 마커를 걷어낸다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.prompts import PURPOSE_VISUAL_RULE_FALLBACK, PURPOSE_VISUAL_RULES
from app.shared import PlannedVisual, ReferenceEvidenceProfile, VisualBudget

# 코드가 그리는 시각자료 전체. 사진(PHOTO)·스크린샷은 여기 없다 — 그건 사진 계획이 맡는다.
ALL_RENDERED_TYPES = frozenset(
    {
        "TABLE",
        "BAR_CHART",
        "LINE_CHART",
        "PIE_CHART",
        "PROCESS_DIAGRAM",
        "INFOGRAPHIC",
    }
)

CHART_TYPES = frozenset({"BAR_CHART", "LINE_CHART", "PIE_CHART"})

# 시각자료 필요성 루브릭(§3). 근거 충분성 30 + 추가 정보 25 + 목적 적합성 20 +
# 비중복성 15 + 모바일 가독성 10 = 100. 85점 미만은 만들지 않는다.
#
# 사진 카드의 80점 게이트(card_selection.MIN_NECESSITY_SCORE)와는 **다른 루브릭**이다.
# 사진은 '촬영 가능한 장면인가'를 묻고, 도표는 '근거가 있는가'를 묻는다.
MIN_VISUAL_NECESSITY_SCORE = 85.0

# 표·그래프 개수는 글 길이 규격과 무관하다(2026-08-03 사용자 결정) — 사진 장수(짧게 2~3장·
# 중간 3~5장)는 사용자 규격이지만, 표·그래프는 근거가 있을 때 AI가 넣을지 말지 판단한다.
# 남는 상한은 목적별 정책(rendered_max)뿐이고 그건 개수 규격이 아니라 근거 원칙이다.


@dataclass(frozen=True)
class PurposePolicy:
    """글 목적 하나가 허용하는 시각자료."""

    purpose: str
    # 이 목적에서 만들 수 있는 렌더링 시각자료 총량(상한).
    rendered_max: int
    allowed_visual_types: frozenset[str]
    # 유형별 상한. 여기 없는 허용 유형은 rendered_max까지 가능하다.
    type_caps: dict[str, int] = field(default_factory=dict)
    body_photos_max: int = 3
    reference_images_max: int = 2
    # 그래프에 실측 수치가 반드시 있어야 하는가. 모든 목적에서 True다(출처 없는 통계 금지).
    charts_need_measured_data: bool = True
    # 사용자가 숫자 자료를 제공했을 때만 열리는 추가 허용 유형.
    unlocked_by_user_data: frozenset[str] = frozenset()
    # 그 자료 자체에 검증된 실측 수치가 실려 있을 때만 열리는 유형. "그래프 0개"가 기본이되
    # "검증된 지표가 있으면 그릴 수 있다"를 함께 지키기 위한 장치다 — 홍보 글에서 지어낸
    # 성장률 그래프는 막고, 출처가 확인된 수치는 살린다.
    unlocked_by_verified_data: frozenset[str] = frozenset()

    @property
    def note(self) -> str:
        """프롬프트가 말하는 것과 같은 문장(app.llm.prompts.PURPOSE_VISUAL_RULES).

        규칙을 두 곳에 적으면 갈라진다 — 프롬프트는 "표 0~1개"라고 하는데 코드는 2개를
        허용하는 식이다. 문장은 프롬프트가 갖고, 상한은 여기가 갖는다.
        """
        return PURPOSE_VISUAL_RULES.get(self.purpose, PURPOSE_VISUAL_RULE_FALLBACK)

    @property
    def forbidden_visual_types(self) -> frozenset[str]:
        return ALL_RENDERED_TYPES - self.allowed_visual_types

    def cap_for(self, visual_type: str) -> int:
        return self.type_caps.get(visual_type, self.rendered_max)


# 목적 라벨은 UI(apps/web/src/constants.ts WRITING_PURPOSES)와 정확히 같아야 한다 —
# 어긋나면 조회가 조용히 빗나가 기본 정책으로 떨어진다.
PURPOSE_POLICIES: dict[str, PurposePolicy] = {
    "입문·소개": PurposePolicy(
        purpose="입문·소개",
        rendered_max=1,
        allowed_visual_types=frozenset({"PROCESS_DIAGRAM", "TABLE"}),
        type_caps={"PROCESS_DIAGRAM": 1, "TABLE": 1},
        body_photos_max=3,
    ),
    "일상·경험 공유": PurposePolicy(
        purpose="일상·경험 공유",
        rendered_max=0,
        allowed_visual_types=frozenset(),
        # 잠금이 풀렸을 때의 상한. rendered_max가 0이라 type_caps가 없으면 열려도 0이 된다.
        type_caps={"TABLE": 1, "BAR_CHART": 1, "LINE_CHART": 1},
        body_photos_max=3,
        unlocked_by_user_data=frozenset({"TABLE", "BAR_CHART", "LINE_CHART"}),
    ),
    "후기·리뷰 작성": PurposePolicy(
        purpose="후기·리뷰 작성",
        rendered_max=1,
        allowed_visual_types=frozenset({"TABLE", "BAR_CHART"}),
        type_caps={"TABLE": 1, "BAR_CHART": 1},
        body_photos_max=4,
    ),
    "비교·추천": PurposePolicy(
        purpose="비교·추천",
        rendered_max=2,
        allowed_visual_types=frozenset({"TABLE", "BAR_CHART"}),
        type_caps={"TABLE": 1, "BAR_CHART": 1},
        body_photos_max=3,
    ),
    "사용법·가이드": PurposePolicy(
        purpose="사용법·가이드",
        rendered_max=1,
        allowed_visual_types=frozenset({"PROCESS_DIAGRAM", "TABLE"}),
        type_caps={"PROCESS_DIAGRAM": 1, "TABLE": 1},
        body_photos_max=3,
    ),
    "트렌드·이슈 소개": PurposePolicy(
        purpose="트렌드·이슈 소개",
        rendered_max=2,
        allowed_visual_types=frozenset({"TABLE", "LINE_CHART", "BAR_CHART"}),
        type_caps={"TABLE": 1, "LINE_CHART": 1, "BAR_CHART": 1},
        body_photos_max=3,
    ),
    "제품·서비스 홍보": PurposePolicy(
        purpose="제품·서비스 홍보",
        rendered_max=1,
        allowed_visual_types=frozenset({"PROCESS_DIAGRAM", "INFOGRAPHIC"}),
        type_caps={"PROCESS_DIAGRAM": 1, "INFOGRAPHIC": 1, "BAR_CHART": 1},
        body_photos_max=4,
        unlocked_by_verified_data=frozenset({"BAR_CHART"}),
    ),
    "정보 전달": PurposePolicy(
        purpose="정보 전달",
        rendered_max=1,
        allowed_visual_types=frozenset(
            {"TABLE", "BAR_CHART", "LINE_CHART", "PROCESS_DIAGRAM"}
        ),
        type_caps={"TABLE": 1, "BAR_CHART": 1, "LINE_CHART": 1, "PROCESS_DIAGRAM": 1},
        body_photos_max=3,
    ),
    "문제 해결": PurposePolicy(
        purpose="문제 해결",
        rendered_max=1,
        allowed_visual_types=frozenset({"PROCESS_DIAGRAM", "TABLE"}),
        type_caps={"PROCESS_DIAGRAM": 1, "TABLE": 1},
        body_photos_max=3,
    ),
}

# 목적을 알 수 없을 때. 가장 보수적인 값을 쓴다 — 모르는 글에 도표를 붙이지 않는다.
DEFAULT_POLICY = PurposePolicy(
    purpose="",
    rendered_max=1,
    allowed_visual_types=frozenset({"TABLE", "PROCESS_DIAGRAM"}),
    type_caps={"TABLE": 1, "PROCESS_DIAGRAM": 1, "BAR_CHART": 1, "LINE_CHART": 1},
    body_photos_max=3,
    # 목적을 모르더라도 출처가 확인된 실측 수치를 그린 그래프까지 버릴 이유는 없다.
    # 막으려는 것은 근거 없는 그래프이지 근거 있는 그래프가 아니다.
    unlocked_by_verified_data=frozenset({"BAR_CHART", "LINE_CHART"}),
)


def purpose_policy(purposes: list[str] | None) -> PurposePolicy:
    """사용자가 고른 첫 목적의 정책. 목적이 없거나 목록에 없으면 보수적인 기본값."""
    for purpose in purposes or []:
        policy = PURPOSE_POLICIES.get((purpose or "").strip())
        if policy is not None:
            return policy
    return DEFAULT_POLICY


def resolve_visual_budget(
    requested: VisualBudget | None,
    *,
    policy: PurposePolicy,
    evidence: ReferenceEvidenceProfile | None = None,
    total_image_cap: int | None = None,
) -> VisualBudget:
    """모델이 요청한 예산을 정책 상한으로 자른다. 모델은 상한을 늘릴 수 없다.

    total_image_cap은 썸네일을 포함한 **사진** 최대 장수(길이별 사용자 결정: 짧게 2~3장·
    중간 3~5장)이다. 사진·첨부 상한을 '썸네일 몫을 뺀 나머지'로 눌러 두고, 최종 합계는
    select_cards가 같은 상한으로 자른다. 표·그래프는 이 총량과 무관하다(2026-08-03
    사용자 결정) — 목적별 근거 원칙(rendered_max)만 남는다.
    """
    body_slots = None if total_image_cap is None else max(0, total_image_cap - 1)
    rendered_ceiling = policy.rendered_max
    photo_ceiling = policy.body_photos_max
    reference_ceiling = policy.reference_images_max
    if body_slots is not None:
        photo_ceiling = min(photo_ceiling, body_slots)
        reference_ceiling = min(reference_ceiling, body_slots)
    if evidence is not None and not evidence.has_references:
        reference_ceiling = 0

    if requested is None:
        return VisualBudget(
            thumbnail=1,
            reference_images_max=reference_ceiling,
            body_photos_max=photo_ceiling,
            rendered_visuals_max=rendered_ceiling,
        )
    return VisualBudget(
        thumbnail=1,
        reference_images_max=max(0, min(requested.reference_images_max, reference_ceiling)),
        body_photos_max=max(0, min(requested.body_photos_max, photo_ceiling)),
        rendered_visuals_max=max(0, min(requested.rendered_visuals_max, rendered_ceiling)),
    )


# ── 근거 없는 자료를 걸러 내는 판정들 ────────────────────────────────────────

# 구체적이지 않은 visualReason. 이런 이유만 적힌 자료는 점수와 무관하게 제외한다.
_VAGUE_REASON_PATTERNS = (
    re.compile(r"한\s*눈에"),
    re.compile(r"이해(를)?\s*(돕|쉽)"),
    re.compile(r"보기\s*(좋|쉽)"),
    re.compile(r"가독성"),
    re.compile(r"정리(하기)?\s*위해$"),
    re.compile(r"시각적으로"),
    re.compile(r"흥미(를)?\s*(끌|유발)"),
)
_MIN_REASON_CHARS = 12

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_NUMBER = re.compile(r"\d")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN.findall(text or "") if len(token) >= 2}


def is_vague_reason(reason: str | None) -> bool:
    """"내용을 한눈에 보여주기 위해" 같은 이유는 이유가 아니다."""
    text = (reason or "").strip()
    if not text:
        return True
    if len(text) < _MIN_REASON_CHARS:
        return True
    return any(pattern.search(text) for pattern in _VAGUE_REASON_PATTERNS)


def visual_text(visual: PlannedVisual) -> str:
    """자료가 담고 있는 문자열 전부. 중복 판정에 쓴다."""
    parts: list[str] = [visual.title or ""]
    for column in visual.columns or []:
        parts.append(column)
    for row in visual.rows or []:
        parts.append(row.name)
        parts.extend(row.cells)
    for step in visual.steps or []:
        parts.append(step.label)
        if step.detail:
            parts.append(step.detail)
    for group in visual.groups or []:
        parts.append(group.name)
        parts.extend(group.items)
    for point in visual.data or []:
        parts.append(point.label)
    if visual.center_topic:
        parts.append(visual.center_topic)
    return " ".join(part for part in parts if part)


# 앞 문단이 자료의 항목을 이 비율 이상 이미 말하고 있으면 '본문을 박스로 옮긴 것'으로 본다.
_REDUNDANCY_RATIO = 0.8


def preceding_paragraph(body: str, visual_id: str) -> str:
    """본문에서 이 자료의 마커 바로 앞 문단. 마커가 없으면 빈 문자열."""
    match = re.search(
        rf"\[\[VISUAL:\s*{re.escape(visual_id)}\s*]]", body or "", flags=re.IGNORECASE
    )
    if match is None:
        return ""
    before = (body or "")[: match.start()]
    blocks = [block.strip() for block in re.split(r"\n\s*\n", before) if block.strip()]
    return blocks[-1] if blocks else ""


def is_redundant(visual: PlannedVisual, body: str) -> bool:
    """자료의 모든 항목이 바로 앞 문단에 이미 있는가.

    수치가 있는 자료는 중복으로 보지 않는다 — 문단이 값을 말했더라도 그래프는 크기 비교를
    더한다. 글자만 옮긴 자료(인포그래픽·설명형 표)에만 적용한다.
    """
    if visual.data:
        return False
    paragraph = preceding_paragraph(body, visual.visual_id)
    if not paragraph:
        return False
    items = _tokens(visual_text(visual)) - _tokens(visual.title or "")
    if len(items) < 4:
        return False
    covered = items & _tokens(paragraph)
    return len(covered) / len(items) >= _REDUNDANCY_RATIO


@dataclass
class VisualGateResult:
    kept: list[PlannedVisual] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


def gate_visuals(
    visuals: list[PlannedVisual] | None,
    *,
    policy: PurposePolicy,
    budget: VisualBudget,
    body: str = "",
    has_user_numeric_data: bool = False,
) -> VisualGateResult:
    """정책·예산·근거로 실제로 그릴 자료만 남긴다.

    순서가 중요하다: 유형 금지 → 근거 없음 → 이유 부실 → 중복 → 유형별 상한 → 총량.
    수치 근거가 있는 자료를 가장 늦게 버린다(총량 초과 시).
    """
    result = VisualGateResult()
    allowed = set(policy.allowed_visual_types)
    if has_user_numeric_data:
        allowed |= set(policy.unlocked_by_user_data)

    rendered_cap = budget.rendered_visuals_max
    if has_user_numeric_data and rendered_cap == 0 and policy.unlocked_by_user_data:
        # 사용자가 실제 숫자 기록을 준 일상 글은 그 수치 하나만 그릴 수 있다.
        rendered_cap = 1

    survivors: list[PlannedVisual] = []
    for visual in visuals or []:
        visual_type = (visual.type or "").upper()
        if visual_type not in ALL_RENDERED_TYPES:
            result.rejections.append(f"{visual.visual_id}: 지원하지 않는 유형 {visual_type}")
            continue
        # 검증된 실측 수치가 실린 그래프는 '그래프 0개' 목적에서도 살린다. 막으려는 것은
        # 지어낸 성장률 그래프이지, 출처가 확인된 수치가 아니다.
        unlocked = (
            visual_type in policy.unlocked_by_verified_data
            and bool(visual.data)
            and bool(visual.source)
        )
        if visual_type not in allowed and not unlocked:
            result.rejections.append(
                f"{visual.visual_id}: '{policy.purpose or '기본'}' 목적에서 허용하지 않는 유형 {visual_type}"
            )
            continue
        if visual_type in CHART_TYPES and policy.charts_need_measured_data and not visual.data:
            result.rejections.append(f"{visual.visual_id}: 실측 수치 없는 그래프")
            continue
        if visual.necessity_score and visual.necessity_score < MIN_VISUAL_NECESSITY_SCORE:
            result.rejections.append(
                f"{visual.visual_id}: 필요성 {visual.necessity_score:.0f}점 "
                f"(<{MIN_VISUAL_NECESSITY_SCORE:.0f})"
            )
            continue
        if visual.visual_reason is not None and is_vague_reason(visual.visual_reason):
            result.rejections.append(f"{visual.visual_id}: 구체적이지 않은 필요 근거")
            continue
        if is_redundant(visual, body):
            result.rejections.append(f"{visual.visual_id}: 바로 앞 문단을 그대로 반복")
            continue
        survivors.append(visual)

    # 유형별 상한.
    per_type: dict[str, int] = {}
    capped: list[PlannedVisual] = []
    for visual in survivors:
        visual_type = visual.type.upper()
        used = per_type.get(visual_type, 0)
        if used >= policy.cap_for(visual_type):
            result.rejections.append(
                f"{visual.visual_id}: {visual_type} 상한 {policy.cap_for(visual_type)}개 초과"
            )
            continue
        per_type[visual_type] = used + 1
        capped.append(visual)

    # 총량. 수치 근거가 있는 자료를 마지막까지 남긴다.
    if len(capped) > rendered_cap:
        ordered = sorted(capped, key=lambda v: (0 if v.data else 1, capped.index(v)))
        kept_ids = {visual.visual_id for visual in ordered[:rendered_cap]}
        for visual in capped:
            if visual.visual_id not in kept_ids:
                result.rejections.append(
                    f"{visual.visual_id}: 목적·길이 상한 {rendered_cap}개 초과"
                )
        capped = [visual for visual in capped if visual.visual_id in kept_ids]

    result.kept = capped
    return result


def has_numeric_user_material(materials) -> bool:
    """사용자 메모에 실제 숫자 기록이 있는가(일상 글의 그래프 잠금 해제 조건).

    날짜·수치가 짝지어 여러 번 나오는 메모만 인정한다 — "3분 정도 걸렸어요" 한 줄로
    그래프를 열어 주면 잠금장치가 아니다.
    """
    from app.shared import ReferenceMaterialType

    for material in materials or []:
        if material.type != ReferenceMaterialType.TEXT:
            continue
        numbers = _NUMBER.findall(material.value or "")
        lines = [line for line in (material.value or "").splitlines() if _NUMBER.search(line)]
        if len(numbers) >= 6 and len(lines) >= 3:
            return True
    return False
