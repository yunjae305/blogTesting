"""참고자료 → 근거 프로필.

참고 URL·이미지·PDF·메모는 지금까지 '첨부물 목록'이었다. 원고 프롬프트에는 파일 이름이,
이미지 생성에는 첫 번째 이미지 하나가 갔다. 그래서 나이키 운동화 사진을 올려도 결과는
'운동화와 관련된 일반적인 사진'이 됐고, 사진이 있다는 이유만으로 "직접 신어 보니" 같은
문장이 나올 여지가 있었다.

여기서는 자료를 세 가지로 나눈다.

- **확인된 것** — 이미지에 실제로 보이는 대상, URL·출처에서 읽은 사실.
- **쓸 수 있는 방식** — 원본 재사용, 썸네일 배경 확장, 제품 중심 크롭.
- **단정하면 안 되는 것** — 구매 여부, 사용 기간, 가격, 착용감.

코드가 만드는 것은 뼈대다(무엇이 몇 장 있고, 사용자가 경험을 적었는가). 모델이 있으면
그 위에 대상·브랜드·확인된 특징을 채운다(``enrich``). 모델이 없어도 프로필은 항상 있다.
"""

from __future__ import annotations

import re

from app.shared import (
    REFERENCE_IMAGE_ROLES,
    ReferenceEvidenceProfile,
    ReferenceImageEvidence,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
)

# 참고 이미지 id 규칙. 카드 브리프의 reference_id와 정확히 같은 문자열을 쓴다 —
# 여러 장을 올렸을 때 어느 장을 쓸지 짝지어야 하기 때문이다.
def reference_image_id(index: int) -> str:
    return f"reference-image-{index + 1}"


# 사용자가 '실제로 겪었다'고 적은 문장. 이 표현이 메모에 있어야 후기·체험 서술이 열린다.
# 첨부가 있다는 사실만으로는 열리지 않는다 — 사진 한 장에 지어낸 1인칭 후기가 붙던 경로다.
EXPERIENCE_EVIDENCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:제가|나는|저는|내가)\s*.{0,18}(?:사용|이용|구매|방문|체험|써\s*봤|먹어\s*봤|신어\s*봤|입어\s*봤)",
        r"직접\s*.{0,12}(?:사용|이용|구매|방문|체험|써\s*봤|측정)",
        r"(?:사용|이용|구매|방문|체험)해\s*보니",
        r"(?:써|가|먹어|입어|신어)\s*본\s*(?:결과|후기|느낌)",
        r"\d+\s*(?:일|주|개월|달|년)\s*(?:동안|간)\s*.{0,10}(?:사용|착용|이용)",
        # '발라 보니', '들어 봤어요'처럼 동사가 소재마다 달라지는 경우. 1인칭 주어 뒤에
        # '~보니/~봤'이 오면 겪은 일을 적은 것이다 — 동사를 전부 열거할 수는 없다.
        r"(?:제가|나는|저는|내가)\s*.{0,20}(?:보니|봤|썼|였)",
        r"직접\s*.{0,14}(?:보니|봤)",
    )
)

# 실제 경험 근거가 없을 때 본문·이미지에서 금지되는 표현.
#
# 2026-08-03 사용자 결정으로 **1인칭 체험·감상 서술 금지는 여기서 뺐다**(직접 구매·내돈내산·
# 사용 기간·영수증 인증·방문 인증·사용 전후 변화). AI 자동 생성 글이 직접 겪은 것처럼 읽히는
# 것이 목적이라는 판단이다. 남은 하나는 체험 표현이 아니라 **측정값 날조** 금지다 —
# 지어낸 수치는 문체 문제가 아니라 사실 문제라 그대로 둔다.
NO_EXPERIENCE_FORBIDDEN_CLAIMS = ("직접 측정한 성능 수치",)

# 참고 이미지에서 끌어내면 안 되는 추정. 이미지에 제품이 보인다고 구매·사용을 단정하지 않는다.
IMAGE_FORBIDDEN_INFERENCES = (
    "실제 구매 가격",
    "사용 기간",
    "사용 감상(착용감·발림성 등)",
    "내구성",
)

# 이미지 역할의 기본 배치. 첫 장이 대표, 둘째가 디테일, 그다음은 장면이다 — 모델이 역할을
# 지정하면 그것이 이긴다.
_DEFAULT_IMAGE_ROLES = ("PRODUCT_ANCHOR", "DETAIL_ANCHOR", "SCENE_ANCHOR")

_ALLOWED_USES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "PRODUCT_ANCHOR": ("원본 재사용", "썸네일 배경 확장", "제품 중심 크롭"),
    "DETAIL_ANCHOR": ("원본 재사용", "소재·디테일 크롭"),
    "SCENE_ANCHOR": ("원본 재사용", "사용 장면 참조"),
    "SCREENSHOT_EVIDENCE": ("원본 재사용",),
    "RECEIPT_EVIDENCE": ("원본 재사용",),
    "CONTEXT_ONLY": ("분위기 참조",),
}


def has_experience_evidence(materials: list[ReferenceMaterial] | None) -> bool:
    """사용자가 실제 경험을 **글로** 남겼는가. 첨부 유무가 아니라 문장으로 판단한다."""
    for material in materials or []:
        if material.type != ReferenceMaterialType.TEXT:
            continue
        text = material.value or ""
        if any(pattern.search(text) for pattern in EXPERIENCE_EVIDENCE_PATTERNS):
            return True
    return False


def reference_images(materials: list[ReferenceMaterial] | None) -> list[ReferenceMaterial]:
    return [
        material
        for material in (materials or [])
        if material.type == ReferenceMaterialType.IMAGE
    ]


def build_profile(
    materials: list[ReferenceMaterial] | None,
    sources: list[SearchSource] | None = None,
    *,
    topic: str = "",
) -> ReferenceEvidenceProfile:
    """모델 없이 만드는 근거 프로필. 확인할 수 있는 사실만 담는다.

    ``primary_entity``를 소재로 채우지 않는다 — 소재는 사용자가 입력한 주제어일 뿐,
    참고자료가 확인해 준 대상이 아니다. 모델 보강(``enrich``)이 실제 대상을 채운다.
    """
    materials = list(materials or [])
    images = reference_images(materials)
    texts = [m for m in materials if m.type == ReferenceMaterialType.TEXT]
    experience = has_experience_evidence(materials)

    roles = [
        ReferenceImageEvidence(
            reference_id=reference_image_id(index),
            role=_DEFAULT_IMAGE_ROLES[min(index, len(_DEFAULT_IMAGE_ROLES) - 1)],
            subject=(material.name or "").strip(),
            allowed_uses=list(
                _ALLOWED_USES_BY_ROLE[
                    _DEFAULT_IMAGE_ROLES[min(index, len(_DEFAULT_IMAGE_ROLES) - 1)]
                ]
            ),
            forbidden_inferences=list(IMAGE_FORBIDDEN_INFERENCES),
        )
        for index, material in enumerate(images)
    ]

    facts: list[str] = []
    for source in sources or []:
        snippet = (source.snippet or "").strip()
        if snippet:
            facts.append(f"{source.title}: {snippet}"[:200])
    for material in texts:
        note = " ".join((material.value or "").split())
        if note:
            facts.append(f"사용자 메모: {note[:200]}")

    return ReferenceEvidenceProfile(
        has_references=bool(materials or sources),
        has_user_experience_evidence=experience,
        primary_entity=None,
        brand=None,
        product_category=None,
        confirmed_attributes=[],
        confirmed_use_scenes=[],
        reference_image_roles=roles,
        source_facts=facts[:8],
        forbidden_claims=[] if experience else list(NO_EXPERIENCE_FORBIDDEN_CLAIMS),
    )


def enrich(
    base: ReferenceEvidenceProfile,
    model_profile: ReferenceEvidenceProfile | None,
) -> ReferenceEvidenceProfile:
    """모델이 읽어 낸 대상·특징을 코드가 만든 뼈대 위에 얹는다.

    **코드가 이기는 값이 있다**: 참고자료의 존재 여부와 실제 경험 근거 여부는 문자열
    판정이라 모델보다 코드가 정확하고, 여기를 모델에 맡기면 "사진이 있으니 경험도 있다"는
    낙관이 그대로 통과한다. 이미지 역할은 모델 값이 있으면 그것을 쓰되, id는 코드가 붙인
    것으로 되돌린다(카드 매핑이 이 id로 이뤄진다).

    소재 정체(content_entity)는 반대로 **모델만이 아는 값**이라 그대로 싣는다 — 코드에는
    '전과자가 프로그램 이름인가 일반 명사인가'를 판정할 근거가 없다. 모델이 판정하지
    못했으면 None으로 남고, 그때 관련 규칙·검사는 통째로 빠진다(예전과 같은 동작).
    """
    if model_profile is None:
        return base

    roles = list(base.reference_image_roles)
    by_id = {role.reference_id: role for role in model_profile.reference_image_roles}
    merged_roles: list[ReferenceImageEvidence] = []
    for role in roles:
        proposed = by_id.get(role.reference_id)
        if proposed is None:
            merged_roles.append(role)
            continue
        proposed_role = (proposed.role or "").upper()
        resolved_role = proposed_role if proposed_role in REFERENCE_IMAGE_ROLES else role.role
        # 영수증·화면 캡처 역할은 사용자가 실제로 그런 이미지를 올렸을 때만 성립한다.
        # 모델이 임의로 붙이면 가짜 증거의 문이 열리므로, 근거가 없으면 되돌린다.
        if resolved_role in ("RECEIPT_EVIDENCE", "SCREENSHOT_EVIDENCE") and not proposed.subject:
            resolved_role = role.role
        merged_roles.append(
            role.model_copy(
                update={
                    "role": resolved_role,
                    "subject": (proposed.subject or role.subject).strip(),
                    "allowed_uses": (
                        proposed.allowed_uses
                        or list(_ALLOWED_USES_BY_ROLE.get(resolved_role, ("원본 재사용",)))
                    ),
                    "forbidden_inferences": (
                        proposed.forbidden_inferences or role.forbidden_inferences
                    ),
                    # 개인정보 좌표는 **모델만이 아는 값**이다. 코드가 만든 뼈대(base)에는
                    # 늘 비어 있으므로, 여기서 싣지 않으면 판정이 통째로 사라져 번호판이
                    # 그대로 발행된다.
                    "private_regions": proposed.private_regions,
                    "privacy_scanned": proposed.privacy_scanned,
                }
            )
        )
        del by_id[role.reference_id]

    forbidden = list(base.forbidden_claims)
    for claim in model_profile.forbidden_claims:
        if claim and claim not in forbidden:
            forbidden.append(claim)

    return base.model_copy(
        update={
            "primary_entity": (model_profile.primary_entity or "").strip() or None,
            "brand": (model_profile.brand or "").strip() or None,
            "product_category": (model_profile.product_category or "").strip() or None,
            "confirmed_attributes": model_profile.confirmed_attributes[:8],
            "confirmed_use_scenes": model_profile.confirmed_use_scenes[:6],
            "reference_image_roles": merged_roles,
            "source_facts": (model_profile.source_facts or base.source_facts)[:8],
            "forbidden_claims": forbidden[:10],
            "content_entity": model_profile.content_entity,
        }
    )
