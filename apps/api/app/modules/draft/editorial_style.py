"""편집·시각 스타일 계획 — 글마다 다른 디자인이 나오게 하는 결정적 선택기.

## 왜 있나

예전에는 시각 정체성이 두 곳에서만 정해졌다: 도표는 네 개 프리셋, 사진은 ``post_id``로
고른 네 개 팔레트. 그래서 뷰티 후기든 벤치마크 비교든 흰 바탕에 파란 포인트로 나왔고,
카테고리가 달라도 그림이 같았다.

## 어떻게 정하나

1. **카테고리와 아키타입**은 의미 판단이라 모델이 정한다(EditorialStylePlan).
   모델이 없거나(테스트 스텁·구형 어댑터) 값이 깨졌으면 여기 규칙이 대신 정한다.
2. **테마·팔레트·레이아웃 변형**은 그 위에서 코드가 결정적으로 고른다. 무작위 색을
   무제한으로 만들지 않는다 — 카테고리마다 검증된 후보 목록이 있고, 그 안에서만
   ``variation_seed``로 뽑는다.
3. 씨앗은 ``postId + generationRevision + category + archetype``이다. 같은 글을 다시
   열면 같은 그림이고(계획을 결과에 저장한다), '다시 생성하기'는 revision이 올라가
   같은 카테고리 안의 **다른** 변형이 나온다.

한 글 안에서는 계열을 하나로 유지하되(chart_theme 하나), 자료마다 레이아웃 변형은
달라질 수 있다 — 표 세 개가 완전히 같은 그림이면 그것도 템플릿이다.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from app.shared import (
    ARTICLE_RHYTHMS,
    BODY_HIGHLIGHT_STYLES,
    CONTENT_CATEGORIES,
    DECORATION_LEVELS,
    EDITORIAL_ARCHETYPES,
    EMOJI_LEVELS,
    THUMBNAIL_COPY_MODES,
    THUMBNAIL_LAYOUTS,
    VISUAL_DENSITY_LEVELS,
    VISUAL_THEMES,
    VOICE_MODES,
    EditorialStylePlan,
    ReferenceEvidenceProfile,
    ThumbnailLayoutPlan,
    WritingDirection,
)

from app.llm.prompts import length_total_image_cap

from .visual_policy import (
    purpose_policy,
    resolve_visual_budget,
)

# 옛 저장 데이터의 프리셋 이름 → 새 테마. 예전 글을 열었을 때 그림이 바뀌지 않게 한다.
LEGACY_THEME_ALIASES: dict[str, str] = {
    "EDITORIAL_NEUTRAL": "EDITORIAL_NEUTRAL",
    "LIFESTYLE_SOFT": "LIFESTYLE_JOURNAL",
    "TECH_MINIMAL": "TECH_BENCHMARK_LIGHT",
    "PROFESSIONAL_DATA": "FINANCE_REPORT",
}


@dataclass(frozen=True)
class CategoryProfile:
    """한 카테고리가 쓸 수 있는 시각 선택지. 여기 없는 값은 이 카테고리에서 나오지 않는다."""

    themes: tuple[str, ...]
    accents: tuple[str, ...]
    photo_languages: tuple[str, ...]
    thumbnail_layouts: tuple[str, ...]
    table_variants: tuple[str, ...]
    # 이 카테고리에서 자연스러운 아키타입(모델이 값을 안 줬을 때의 후보).
    archetypes: tuple[str, ...]
    voice_modes: tuple[str, ...]
    emoji_level: str
    decoration_level: str
    # 이미지 모델에 넘기는 색·광원 방향. post_id 팔레트 4종을 대체한다.
    colour_directions: tuple[str, ...]
    # 카테고리 차원의 금지 시각자료(목적 정책과 합쳐진다).
    forbidden_visual_types: tuple[str, ...] = ()


_NEUTRAL_TABLES = ("STANDARD_GRID", "FEATURE_MATRIX", "COMPACT_MOBILE")


CATEGORY_PROFILES: dict[str, CategoryProfile] = {
    "DAILY_LIFE": CategoryProfile(
        themes=("LIFESTYLE_JOURNAL", "EDITORIAL_NEUTRAL"),
        accents=("TERRACOTTA_SAND", "AMBER_CHARCOAL"),
        photo_languages=("NATURAL_DAILY",),
        thumbnail_layouts=(
            "NO_COPY_EDITORIAL_PHOTO",
            "SMALL_LABEL_BOTTOM_LEFT",
            "COPY_BOTTOM_SUBJECT_TOP",
        ),
        table_variants=("COMPACT_MOBILE",),
        archetypes=("DAILY_JOURNAL", "PERSONAL_EPISODE"),
        voice_modes=("WARM_PERSONAL",),
        emoji_level="MINIMAL",
        decoration_level="LOW",
        colour_directions=(
            "Warm neutral palette: soft afternoon daylight, beige, oak and cream "
            "surfaces, one muted terracotta accent.",
            "Warm evening palette: low golden light, walnut and linen surfaces, one "
            "amber accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "BAR_CHART", "LINE_CHART", "INFOGRAPHIC"),
    ),
    "BEAUTY": CategoryProfile(
        themes=("BEAUTY_EDITORIAL", "LIFESTYLE_JOURNAL"),
        accents=("ROSE_CREAM", "VIOLET_SLATE"),
        photo_languages=("SOFT_BEAUTY_DESK", "PRODUCT_STUDIO_NATURAL"),
        thumbnail_layouts=(
            "PRODUCT_CUTOUT_WITH_SIDE_COPY",
            "NO_COPY_EDITORIAL_PHOTO",
            "SMALL_LABEL_TOP_LEFT",
        ),
        table_variants=("PROS_CONS_CARDS", "COMPACT_MOBILE", "STANDARD_GRID"),
        archetypes=("FIELD_REVIEW", "EXPERT_EXPLAINER", "PRODUCT_TEST_LOG"),
        voice_modes=("WARM_PERSONAL", "FRIENDLY_COACH"),
        emoji_level="MINIMAL",
        decoration_level="MEDIUM",
        colour_directions=(
            "Soft beauty palette: diffused window light on a dressing table, cream, "
            "pale rose and warm ivory surfaces, one dusty-pink accent.",
            "Clean cosmetic palette: bright soft daylight, milky white and lilac "
            "surfaces, one muted mauve accent, gentle shadows.",
        ),
        forbidden_visual_types=("PIE_CHART", "LINE_CHART"),
    ),
    "FASHION": CategoryProfile(
        themes=("BRAND_MINIMAL", "TREND_MAGAZINE"),
        accents=("CORAL_INK", "AMBER_CHARCOAL"),
        photo_languages=("EDITORIAL_FASHION", "PRODUCT_STUDIO_NATURAL"),
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "NO_COPY_EDITORIAL_PHOTO",
            "SMALL_LABEL_BOTTOM_LEFT",
        ),
        table_variants=("SPEC_SHEET", "PROS_CONS_CARDS"),
        archetypes=("FIELD_REVIEW", "BRAND_STORY", "TREND_COMMENTARY"),
        voice_modes=("WARM_PERSONAL", "CALM_OBSERVER"),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Editorial daylight palette: even overcast light, off-white, stone and "
            "denim surfaces, one deep ink accent.",
            "Warm studio-daylight palette: soft window light, sand and camel "
            "surfaces, one rust accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "LINE_CHART", "BAR_CHART"),
    ),
    "FOOD": CategoryProfile(
        themes=("FOOD_TRAVEL", "LIFESTYLE_JOURNAL"),
        accents=("TERRACOTTA_SAND", "AMBER_CHARCOAL"),
        photo_languages=("WARM_FOOD_TABLE", "LOCAL_STREET"),
        thumbnail_layouts=(
            "NO_COPY_EDITORIAL_PHOTO",
            "COPY_BOTTOM_SUBJECT_TOP",
            "SMALL_LABEL_BOTTOM_LEFT",
        ),
        table_variants=("COMPACT_MOBILE", "SPEC_SHEET"),
        archetypes=("FIELD_REVIEW", "LOCAL_GUIDE", "PERSONAL_EPISODE"),
        voice_modes=("WARM_PERSONAL",),
        emoji_level="MINIMAL",
        decoration_level="LOW",
        colour_directions=(
            "Warm table palette: window light across a wooden table, cream ceramics, "
            "one deep russet accent.",
            "Evening restaurant palette: warm tungsten pools of light, dark wood and "
            "brass surfaces, one amber accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "BAR_CHART", "LINE_CHART"),
    ),
    "TRAVEL": CategoryProfile(
        themes=("FOOD_TRAVEL", "TREND_MAGAZINE"),
        accents=("FOREST_STONE", "TERRACOTTA_SAND"),
        photo_languages=("TRAVEL_ON_LOCATION", "LOCAL_STREET"),
        thumbnail_layouts=(
            "NO_COPY_EDITORIAL_PHOTO",
            "COPY_BOTTOM_SUBJECT_TOP",
            "SMALL_LABEL_TOP_LEFT",
        ),
        table_variants=("COMPACT_MOBILE", "SPEC_SHEET"),
        archetypes=("LOCAL_GUIDE", "PERSONAL_EPISODE", "FIELD_REVIEW"),
        voice_modes=("WARM_PERSONAL", "FRIENDLY_COACH"),
        emoji_level="MINIMAL",
        decoration_level="LOW",
        colour_directions=(
            "Open daylight palette: clear midday sun, sandstone, sea blue and green "
            "surfaces, one deep teal accent.",
            "Golden-hour travel palette: low warm sun, weathered stone and terracotta "
            "surfaces, one burnt-orange accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "BAR_CHART", "LINE_CHART"),
    ),
    "FITNESS_SPORTS": CategoryProfile(
        themes=("FITNESS_PERFORMANCE", "EDITORIAL_NEUTRAL"),
        accents=("FOREST_STONE", "CORAL_INK"),
        photo_languages=("DYNAMIC_ACTION",),
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "COPY_RIGHT_SUBJECT_LEFT",
            "SMALL_LABEL_BOTTOM_LEFT",
        ),
        table_variants=("WINNER_HIGHLIGHT", "SPEC_SHEET", "COMPACT_MOBILE"),
        archetypes=("PRODUCT_TEST_LOG", "FIELD_REVIEW", "EXPERT_EXPLAINER"),
        voice_modes=("FRIENDLY_COACH", "DIRECT_EXPERT"),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "High-contrast daylight palette: hard directional sun, asphalt grey and "
            "chalk white surfaces, one vivid signal accent.",
            "Indoor gym palette: cool overhead light, rubber black and steel grey "
            "surfaces, one lime accent.",
        ),
        forbidden_visual_types=("PIE_CHART",),
    ),
    "TECH_IT": CategoryProfile(
        themes=("TECH_BENCHMARK_LIGHT", "TECH_BENCHMARK_DARK"),
        accents=("CYAN_NAVY", "VIOLET_SLATE"),
        photo_languages=("REAL_WORKDESK_TEST", "PRODUCT_STUDIO_NATURAL"),
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "COPY_RIGHT_SUBJECT_LEFT",
            "SMALL_LABEL_TOP_LEFT",
        ),
        table_variants=("WINNER_HIGHLIGHT", "SPEC_SHEET", "FEATURE_MATRIX"),
        archetypes=("COMPARISON_LAB", "PRODUCT_TEST_LOG", "EXPERT_EXPLAINER"),
        voice_modes=("DIRECT_EXPERT",),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Cool desk palette: overcast window light, charcoal, slate and pale grey "
            "surfaces, one deep navy accent.",
            "Neutral workshop palette: soft daylight, light wood and matte black "
            "surfaces, one muted cyan accent.",
        ),
        forbidden_visual_types=("PIE_CHART",),
    ),
    "GAMING_ESPORTS": CategoryProfile(
        themes=("GAMING_ESPORTS", "TECH_BENCHMARK_DARK"),
        accents=("VIOLET_SLATE", "CORAL_INK"),
        photo_languages=("GAMING_ARENA", "REAL_WORKDESK_TEST"),
        thumbnail_layouts=(
            "COPY_RIGHT_SUBJECT_LEFT",
            "COPY_LEFT_SUBJECT_RIGHT",
            "SMALL_LABEL_BOTTOM_LEFT",
        ),
        table_variants=("WINNER_HIGHLIGHT", "FEATURE_MATRIX"),
        archetypes=("ISSUE_BRIEF", "TREND_COMMENTARY", "EXPERT_EXPLAINER"),
        voice_modes=("CALM_OBSERVER", "DIRECT_EXPERT"),
        emoji_level="NONE",
        decoration_level="MEDIUM",
        colour_directions=(
            "Arena palette: stage lighting across a crowd, deep blue shadows and warm "
            "spot highlights, one magenta accent.",
            "Night desk palette: dim room lit by monitors, charcoal and graphite "
            "surfaces, one cool violet accent.",
        ),
        forbidden_visual_types=("PIE_CHART",),
    ),
    "BUSINESS_FINANCE": CategoryProfile(
        themes=("FINANCE_REPORT", "EDITORIAL_NEUTRAL"),
        accents=("CYAN_NAVY", "FOREST_STONE"),
        photo_languages=("CLEAN_BUSINESS", "REAL_WORKDESK_TEST"),
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "SMALL_LABEL_TOP_LEFT",
            "COPY_TOP_SUBJECT_BOTTOM",
        ),
        table_variants=("SPEC_SHEET", "STANDARD_GRID", "WINNER_HIGHLIGHT"),
        archetypes=("EXPERT_EXPLAINER", "ISSUE_BRIEF", "COMPARISON_LAB"),
        voice_modes=("DIRECT_EXPERT", "CALM_OBSERVER"),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Quiet office palette: north-facing daylight, paper white and grey-blue "
            "surfaces, one navy accent.",
            "Warm meeting-room palette: soft daylight, oak table and off-white walls, "
            "one deep green accent.",
        ),
    ),
    "EDUCATION": CategoryProfile(
        themes=("EDUCATION_GUIDE", "EDITORIAL_NEUTRAL"),
        accents=("FOREST_STONE", "CYAN_NAVY"),
        photo_languages=("STUDY_DESK", "NATURAL_DAILY"),
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "COPY_TOP_SUBJECT_BOTTOM",
            "SMALL_LABEL_TOP_LEFT",
        ),
        table_variants=("STANDARD_GRID", "COMPACT_MOBILE"),
        archetypes=("STEP_BY_STEP_TUTORIAL", "FAQ_GUIDE", "EXPERT_EXPLAINER"),
        voice_modes=("FRIENDLY_COACH",),
        emoji_level="MINIMAL",
        decoration_level="LOW",
        colour_directions=(
            "Study-desk palette: warm lamp plus daylight, paper, pencil wood and "
            "linen surfaces, one muted green accent.",
            "Classroom palette: even daylight, chalk white and light wood surfaces, "
            "one soft blue accent.",
        ),
        forbidden_visual_types=("PIE_CHART",),
    ),
    "LOCAL_LIFE": CategoryProfile(
        themes=("FOOD_TRAVEL", "LIFESTYLE_JOURNAL"),
        accents=("TERRACOTTA_SAND", "FOREST_STONE"),
        photo_languages=("LOCAL_STREET", "NATURAL_DAILY"),
        thumbnail_layouts=(
            "NO_COPY_EDITORIAL_PHOTO",
            "SMALL_LABEL_BOTTOM_LEFT",
            "COPY_BOTTOM_SUBJECT_TOP",
        ),
        table_variants=("COMPACT_MOBILE", "SPEC_SHEET"),
        archetypes=("LOCAL_GUIDE", "FIELD_REVIEW"),
        voice_modes=("WARM_PERSONAL", "FRIENDLY_COACH"),
        emoji_level="MINIMAL",
        decoration_level="LOW",
        colour_directions=(
            "Neighbourhood daylight palette: afternoon sun on a street, brick, "
            "painted signage and asphalt, one deep red accent.",
            "Overcast street palette: soft grey light, concrete and glass surfaces, "
            "one muted blue accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "BAR_CHART", "LINE_CHART"),
    ),
    "BRAND_PRODUCT": CategoryProfile(
        themes=("BRAND_MINIMAL", "EDITORIAL_NEUTRAL"),
        accents=("AMBER_CHARCOAL", "CYAN_NAVY"),
        photo_languages=("PRODUCT_STUDIO_NATURAL", "REAL_WORKDESK_TEST"),
        thumbnail_layouts=(
            "PRODUCT_CUTOUT_WITH_SIDE_COPY",
            "COPY_LEFT_SUBJECT_RIGHT",
            "SMALL_LABEL_BOTTOM_LEFT",
        ),
        table_variants=("SPEC_SHEET", "TWO_PRODUCT_SPLIT", "STANDARD_GRID"),
        archetypes=("BRAND_STORY", "EXPERT_EXPLAINER", "FIELD_REVIEW"),
        voice_modes=("BRAND_VOICE", "DIRECT_EXPERT"),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Quiet product palette: soft directional daylight, matte paper and stone "
            "surfaces, one restrained brand accent.",
            "Workshop palette: raking window light, raw wood and metal surfaces, one "
            "deep amber accent.",
        ),
        forbidden_visual_types=("PIE_CHART", "LINE_CHART", "BAR_CHART"),
    ),
    "TREND_NEWS": CategoryProfile(
        themes=("TREND_MAGAZINE", "EDITORIAL_NEUTRAL"),
        accents=("CORAL_INK", "CYAN_NAVY"),
        photo_languages=("LOCAL_STREET", "CLEAN_BUSINESS"),
        thumbnail_layouts=(
            "COPY_TOP_SUBJECT_BOTTOM",
            "SMALL_LABEL_TOP_LEFT",
            "COPY_LEFT_SUBJECT_RIGHT",
        ),
        table_variants=("STANDARD_GRID", "FEATURE_MATRIX"),
        archetypes=("TREND_COMMENTARY", "ISSUE_BRIEF"),
        voice_modes=("CALM_OBSERVER",),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Reportage palette: available daylight, muted urban colour, one strong "
            "red accent.",
            "Cool editorial palette: overcast light, grey and off-white surfaces, one "
            "deep blue accent.",
        ),
        forbidden_visual_types=("PIE_CHART",),
    ),
    "OTHER": CategoryProfile(
        themes=("EDITORIAL_NEUTRAL", "TREND_MAGAZINE"),
        accents=("CYAN_NAVY", "FOREST_STONE"),
        photo_languages=("NATURAL_DAILY", "CLEAN_BUSINESS"),
        # 문구 없는 썸네일은 일상·뷰티·패션·음식·여행·지역 글에서만 후보다. 무엇에 대한
        # 글인지 모르는 상태에서 문구를 빼면 피드에서 아무것도 전달하지 못한다.
        thumbnail_layouts=(
            "COPY_LEFT_SUBJECT_RIGHT",
            "SMALL_LABEL_TOP_LEFT",
            "COPY_TOP_SUBJECT_BOTTOM",
        ),
        table_variants=_NEUTRAL_TABLES,
        archetypes=("EXPERT_EXPLAINER", "FAQ_GUIDE"),
        voice_modes=("DIRECT_EXPERT", "CALM_OBSERVER"),
        emoji_level="NONE",
        decoration_level="LOW",
        colour_directions=(
            "Warm neutral palette: soft daylight, beige, oak and cream surfaces, one "
            "muted terracotta accent.",
            "Cool neutral palette: overcast window light, grey, white and pale-blue "
            "surfaces, one deep navy accent.",
        ),
    ),
}


def category_profile(category: str | None) -> CategoryProfile:
    return CATEGORY_PROFILES.get((category or "").upper(), CATEGORY_PROFILES["OTHER"])


def _seed_number(seed: str, salt: str) -> int:
    return zlib.crc32(f"{seed}|{salt}".encode())


def pick(options: tuple[str, ...] | list[str], seed: str, salt: str) -> str:
    """후보 안에서 결정적으로 하나 고른다. 같은 (seed, salt)면 항상 같은 값이다."""
    if not options:
        return ""
    return options[_seed_number(seed, salt) % len(options)]


def variation_seed_for(post_id: str, revision: int, category: str, archetype: str) -> str:
    """씨앗은 글·회차·카테고리·아키타입의 조합이다.

    post_id만 쓰면 카테고리가 달라도 같은 팔레트가 나오고, 회차를 빼면 '다시 생성하기'가
    똑같은 디자인을 낸다. 넷을 합쳐야 "같은 글은 같게, 다시 만들면 다르게, 카테고리가
    다르면 다르게"가 동시에 성립한다.
    """
    return f"{post_id}:{revision}:{(category or 'OTHER').upper()}:{(archetype or '').upper()}"


# ── 코드 폴백: 모델 없이도 카테고리를 정한다 ─────────────────────────────────
#
# 어댑터가 편집 스타일 계획을 지원하지 않거나 호출이 실패해도 글은 나와야 한다. 그때는
# 소재·목적·페르소나 문자열의 신호로 카테고리를 고른다. 정확도는 모델보다 낮지만,
# "모든 글이 같은 파란 도표"보다는 확실히 낫다.
_CATEGORY_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("BEAUTY", ("화장품", "뷰티", "스킨케어", "메이크업", "립", "쿠션", "에센스", "선크림", "향수")),
    ("FASHION", ("패션", "코디", "의류", "옷", "신발", "운동화", "가방", "착장", "룩북")),
    ("FOOD", ("맛집", "음식", "레시피", "카페", "베이커리", "디저트", "요리", "식당")),
    ("TRAVEL", ("여행", "여행지", "숙소", "호텔", "항공", "관광", "코스", "투어")),
    ("FITNESS_SPORTS", ("운동", "헬스", "러닝", "다이어트", "요가", "필라테스", "축구", "야구", "마라톤")),
    ("TECH_IT", ("노트북", "스마트폰", "가전", "it", "ai", "소프트웨어", "앱", "카메라", "모니터", "충전")),
    ("GAMING_ESPORTS", ("게임", "e스포츠", "이스포츠", "롤", "리그오브레전드", "배틀그라운드", "플레이")),
    ("BUSINESS_FINANCE", ("재테크", "투자", "주식", "부동산", "세금", "연금", "창업", "마케팅", "매출")),
    ("EDUCATION", ("공부", "학습", "강의", "자격증", "시험", "교육", "입시", "독학")),
    ("LOCAL_LIFE", ("동네", "지역", "생활", "주차", "민원", "복지", "이사", "청소")),
    ("TREND_NEWS", ("트렌드", "이슈", "논란", "화제", "근황", "발표", "출시일")),
)

# 목적이 곧 카테고리는 아니지만, 소재 신호가 없을 때의 마지막 힌트는 된다.
_PURPOSE_CATEGORY_HINT: dict[str, str] = {
    "일상·경험 공유": "DAILY_LIFE",
    "트렌드·이슈 소개": "TREND_NEWS",
    "제품·서비스 홍보": "BRAND_PRODUCT",
    "사용법·가이드": "EDUCATION",
    "문제 해결": "EDUCATION",
}

# 목적이 정하는 기본 아키타입. 페르소나보다 목적이 앞선다(§4 확정 규칙).
_PURPOSE_ARCHETYPE: dict[str, str] = {
    "정보 전달": "EXPERT_EXPLAINER",
    "입문·소개": "EXPERT_EXPLAINER",
    "일상·경험 공유": "DAILY_JOURNAL",
    "사용법·가이드": "STEP_BY_STEP_TUTORIAL",
    "후기·리뷰 작성": "FIELD_REVIEW",
    "비교·추천": "COMPARISON_LAB",
    "문제 해결": "STEP_BY_STEP_TUTORIAL",
    "트렌드·이슈 소개": "TREND_COMMENTARY",
    "제품·서비스 홍보": "BRAND_STORY",
}

# 아키타입이 정하는 글의 리듬. 도입부가 늘 같은 순서로 시작하지 않게 하는 값이다.
ARCHETYPE_RHYTHM: dict[str, str] = {
    "DAILY_JOURNAL": "SCENE_FIRST",
    "PERSONAL_EPISODE": "SCENE_FIRST",
    "FIELD_REVIEW": "PROBLEM_FIRST",
    "PRODUCT_TEST_LOG": "CRITERIA_FIRST",
    "COMPARISON_LAB": "ANSWER_FIRST",
    "EXPERT_EXPLAINER": "ANSWER_FIRST",
    "STEP_BY_STEP_TUTORIAL": "TIMELINE",
    "ISSUE_BRIEF": "FACT_THEN_MEANING",
    "TREND_COMMENTARY": "FACT_THEN_MEANING",
    "BRAND_STORY": "SCENE_FIRST",
    "LOCAL_GUIDE": "QUESTION_ANSWER",
    "FAQ_GUIDE": "QUESTION_ANSWER",
}

def guess_category(topic: str, subject: str | None, purposes: list[str]) -> str:
    """소재·주제 문자열로 카테고리를 추정한다(모델 계획이 없을 때의 폴백)."""
    haystack = f"{topic} {subject or ''}".lower()
    for category, signals in _CATEGORY_SIGNALS:
        if any(signal in haystack for signal in signals):
            return category
    for purpose in purposes:
        hint = _PURPOSE_CATEGORY_HINT.get(purpose)
        if hint:
            return hint
    return "OTHER"


def guess_archetype(purposes: list[str], category: str, seed: str) -> str:
    for purpose in purposes:
        archetype = _PURPOSE_ARCHETYPE.get(purpose)
        if archetype:
            return archetype
    return pick(category_profile(category).archetypes, seed, "archetype")


def _clean_enum(value: str | None, allowed: tuple[str, ...], fallback: str) -> str:
    text = (value or "").strip().upper()
    return text if text in allowed else fallback


def normalize_style_plan(
    plan: EditorialStylePlan | None,
    *,
    post_id: str,
    revision: int,
    topic: str,
    subject: str | None,
    purposes: list[str],
    article_length: str,
    evidence: ReferenceEvidenceProfile | None = None,
) -> EditorialStylePlan:
    """모델 계획(또는 없음)을 실제로 쓸 수 있는 계획으로 확정한다.

    여기서 하는 일은 넷이다.
    1. 알 수 없는 값 정리 — 카테고리·아키타입이 목록 밖이면 코드 추정으로 되돌린다.
    2. (2026-08-03 사용자 결정으로 '경험 강등'을 없앴다 — 자리 번호는 아래를 그대로 둔다.)
    3. 시각 선택 확정 — 테마·팔레트·레이아웃을 카테고리 후보 안에서 씨앗으로 고른다.
    4. 예산 강제 — 목적 정책과 글 길이 상한으로 시각자료 개수를 자른다(상한이지 최소가 아니다).
    """
    purposes = [p for p in (purposes or []) if p]
    fallback_category = guess_category(topic, subject, purposes)
    category = _clean_enum(
        plan.content_category if plan else None, CONTENT_CATEGORIES, fallback_category
    )

    provisional_seed = variation_seed_for(post_id, revision, category, "")
    fallback_archetype = guess_archetype(purposes, category, provisional_seed)
    archetype = _clean_enum(
        plan.editorial_archetype if plan else None, EDITORIAL_ARCHETYPES, fallback_archetype
    )

    # 2026-08-03 사용자 결정: 경험 자료가 없다고 후기·체험 아키타입을 설명형으로 강등하지
    # 않는다. AI 자동 생성의 목적 자체가 직접 겪은 것처럼 읽히는 글이라는 판단이다.
    # (has_experience는 아래 편집 지시·사진 계획에서 계속 쓰이므로 값은 그대로 구한다.)
    has_experience = bool(evidence and evidence.has_user_experience_evidence)

    seed = variation_seed_for(post_id, revision, category, archetype)
    profile = category_profile(category)
    policy = purpose_policy(purposes)

    # 모델이 고른 테마는 목록 안에 있을 때만 존중한다(옛 프리셋 이름은 새 테마로 옮긴다).
    raw_theme = (plan.chart_theme if plan else "").strip().upper()
    raw_theme = LEGACY_THEME_ALIASES.get(raw_theme, raw_theme)
    theme = raw_theme if raw_theme in VISUAL_THEMES else pick(profile.themes, seed, "theme")

    table_theme = pick(profile.table_variants, seed, "table")
    accent = pick(profile.accents, seed, "accent")
    photo_language = pick(profile.photo_languages, seed, "photo")

    # 문구 없는 썸네일을 허용하는 카테고리는 후보 목록에 NO_COPY_EDITORIAL_PHOTO가 들어 있다.
    thumbnail_layout = _clean_enum(
        plan.thumbnail_layout if plan else None, THUMBNAIL_LAYOUTS, ""
    ) or pick(profile.thumbnail_layouts, seed, "thumbnail")
    copy_mode = (
        "NONE"
        if thumbnail_layout == "NO_COPY_EDITORIAL_PHOTO"
        else _clean_enum(
            plan.thumbnail_copy_mode if plan else None,
            THUMBNAIL_COPY_MODES,
            "SHORT_LABEL",
        )
    )

    allowed = sorted(policy.allowed_visual_types - set(profile.forbidden_visual_types))
    forbidden = sorted(
        (policy.forbidden_visual_types | set(profile.forbidden_visual_types))
    )

    budget = resolve_visual_budget(
        plan.visual_budget if plan else None,
        policy=policy,
        evidence=evidence,
        # 사진 장수는 글 길이가 정한다: 짧게 2~3장, 중간 3~5장(썸네일 포함).
        # 표·그래프는 이 총량과 무관하다(2026-08-03 사용자 결정).
        total_image_cap=length_total_image_cap(article_length),
    )

    return EditorialStylePlan(
        content_category=category,
        editorial_archetype=archetype,
        voice_mode=_clean_enum(
            plan.voice_mode if plan else None,
            VOICE_MODES,
            pick(profile.voice_modes, seed, "voice"),
        ),
        visual_density=_clean_enum(
            plan.visual_density if plan else None,
            VISUAL_DENSITY_LEVELS,
            "NONE" if budget.rendered_visuals_max == 0 else "LOW",
        ),
        emoji_level=_clean_enum(
            plan.emoji_level if plan else None, EMOJI_LEVELS, profile.emoji_level
        ),
        decoration_level=_clean_enum(
            plan.decoration_level if plan else None,
            DECORATION_LEVELS,
            profile.decoration_level,
        ),
        article_rhythm=_clean_enum(
            plan.article_rhythm if plan else None,
            ARTICLE_RHYTHMS,
            ARCHETYPE_RHYTHM.get(archetype, "ANSWER_FIRST"),
        ),
        photo_language=photo_language,
        thumbnail_layout=thumbnail_layout,
        thumbnail_copy_mode=copy_mode,
        body_highlight_style=_clean_enum(
            plan.body_highlight_style if plan else None,
            BODY_HIGHLIGHT_STYLES,
            pick(("BOLD_ONLY", "BOLD_AND_HIGHLIGHT", "MINIMAL"), seed, "highlight"),
        ),
        chart_theme=theme,
        table_theme=table_theme,
        accent_family=accent,
        allowed_visual_types=allowed,
        forbidden_visual_types=forbidden,
        visual_budget=budget,
        variation_seed=seed,
        generation_revision=revision,
        writing_direction=_resolve_writing_direction(
            plan.writing_direction if plan else None, has_experience=has_experience
        ),
    )


def _resolve_writing_direction(
    direction: WritingDirection | None, *, has_experience: bool
) -> WritingDirection | None:
    """모델이 쓴 편집 지시를 그대로 쓴다.

    2026-08-03 사용자 결정 전에는 참고자료에 실제 경험이 없으면 1인칭 정책을 코드 문장으로
    덮어썼다("1인칭 체험 표현을 사용하지 않는다"). 이제 덮어쓰지 않는다 — 1인칭 체험·감상
    서술이 허용되므로 문체는 모델과 페르소나가 정한다.

    has_experience 인자는 호출부 계약을 유지하려고 남겨 뒀다.
    """
    return direction


def colour_direction_for(plan: EditorialStylePlan) -> str:
    """이미지 모델에 넘길 색·광원 방향 한 줄. 글 하나에 하나이며, 카테고리가 바뀌면 바뀐다."""
    profile = category_profile(plan.content_category)
    return pick(profile.colour_directions, plan.variation_seed or plan.content_category, "colour")


def thumbnail_layout_plan_for(
    plan: EditorialStylePlan | None,
    copy_lines: list[str],
    face_safe: bool = False,
) -> ThumbnailLayoutPlan:
    """새 대표 썸네일을 레퍼런스형 중앙 제목 박스로 확정한다.

    편집 스타일의 카테고리·색 계열은 그대로 보존하지만 썸네일의 실제 합성 규격만
    통일한다. 저장된 구형 ThumbnailLayoutPlan을 직접 렌더링하는 경로는 imaging에서 계속
    지원하므로 기존 글의 재렌더 결과는 깨지지 않는다.

    face_safe=True(실존 인물·캐릭터가 주요 피사체)면 제목 박스를 아래 띠로 내린다.
    중앙 박스는 정확히 얼굴 위에 앉아 "누구인지 보여 준다"는 목적을 스스로 지운다.
    """
    show_copy = bool(copy_lines)
    if face_safe:
        return ThumbnailLayoutPlan(
            layout="COPY_BOTTOM_SUBJECT_TOP",
            subject_zone="TOP_CENTER",
            copy_zone="BOTTOM_CENTER",
            copy_alignment="CENTER",
            copy_mode="SHORT_LABEL",
            copy_lines=list(copy_lines) if show_copy else [],
            scrim_style="LOCAL_ROUNDED",
            accent_style="NONE",
            show_copy=show_copy,
        )
    return ThumbnailLayoutPlan(
        layout="CENTER_COPY_ON_NEGATIVE_SPACE",
        subject_zone="CENTER",
        copy_zone="CENTER",
        copy_alignment="CENTER",
        copy_mode="SHORT_LABEL",
        copy_lines=list(copy_lines) if show_copy else [],
        scrim_style="LOCAL_ROUNDED",
        accent_style="NONE",
        show_copy=show_copy,
    )
