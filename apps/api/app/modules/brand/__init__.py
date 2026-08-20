"""브랜드 자료 — 글마다 반복해서 들어가는 회사·서비스 정보.

2026-08-11부터 브랜드는 **별도 화면이 아니라 새 글 작성의 선택 항목**이다. 그래서 여기에
있던 자동 생성 서비스(`BrandAutoPostService`)와 브랜드 전용 글 생성 입력
(`brand_post_input`·`brand_topic`)은 없앴다 — 글은 `POST /posts` 한 곳에서만 만들어지고,
브랜드가 하는 일은 그 입력에 자기 자료를 얹는 것뿐이다(`merge_brand_materials`).

2026-08-19부터 브랜드는 **소재와 함께** 고를 수 있다. 그래서 여기에 두 가지가 늘었다:
어느 역할로 쓰는 글인지 정하는 `brand_mode_for`(FOCUS·UTILITY)와, 그 소재에 브랜드를
얹는 것이 자연스러운지 재는 `evaluate_brand_fit`(A·B·C)이다.
"""

from .defaults import (
    DEFAULT_BRAND_ID,
    DEFAULT_BRAND_NAME,
    DEFAULTS_REVISION,
    default_brand_body,
)
from .fit import (
    BRAND_FIT_DIRECT,
    BRAND_FIT_FORCED,
    BRAND_FIT_GRADES,
    BRAND_FIT_SITUATIONAL,
    BrandFit,
    BrandFitMatch,
    brand_use_case_lines,
    evaluate_brand_fit,
    use_case_brief,
)
from .repository import (
    COLLECTION,
    BrandRepository,
    InMemoryBrandRepository,
    MongoBrandRepository,
)
from .service import (
    BRAND_MATERIAL_ORIGIN,
    BRAND_POST_MAX_REFERENCE_MATERIALS,
    BrandService,
    brand_brief,
    brand_mode_for,
    brand_reference_materials,
    fit_context_of,
    merge_brand_materials,
    with_brand_materials,
)
from .validation import validate_brand_body

__all__ = [
    "DEFAULT_BRAND_ID",
    "DEFAULT_BRAND_NAME",
    "BRAND_FIT_DIRECT",
    "BRAND_FIT_FORCED",
    "BRAND_FIT_GRADES",
    "BRAND_FIT_SITUATIONAL",
    "BRAND_MATERIAL_ORIGIN",
    "BRAND_POST_MAX_REFERENCE_MATERIALS",
    "COLLECTION",
    "BrandFit",
    "BrandFitMatch",
    "BrandRepository",
    "BrandService",
    "InMemoryBrandRepository",
    "MongoBrandRepository",
    "brand_brief",
    "DEFAULTS_REVISION",
    "default_brand_body",
    "brand_mode_for",
    "brand_reference_materials",
    "brand_use_case_lines",
    "evaluate_brand_fit",
    "fit_context_of",
    "merge_brand_materials",
    "use_case_brief",
    "validate_brand_body",
    "with_brand_materials",
]
