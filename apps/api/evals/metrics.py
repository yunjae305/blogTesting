"""기준선 지표. 전부 순수 함수다 — 네트워크·모델 호출이 없어 단위 테스트로 검증한다.

지표는 품질을 정의하는 점수가 아니다. **회귀를 발견하기 위한 보조 지표**이고, 그래서 하나의
종합 점수로 합치지 않는다(단일 "사람이 쓴 글 점수"를 만들지 않는다는 규칙). 각 항목은 따로
보고되고, 수정 프롬프트에도 따로 전달된다.

기존 검사를 다시 구현하지 않고 그대로 쓴다: `check_draft`, `body_char_count`,
`_repeated_ngram_rate`, `_cliche_hits`. 여기서 새로 계산하는 것은 **지금 코드에 없는 지표**뿐이다
(도입부 상투구, 연결어 반복, 제목–첫문장 중복, 소제목 문법 균일성, 문단 길이 균일성,
결론–본문 중복, SEO 과밀, 근거 없는 수치).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field

from app.llm.prompts import (
    CLICHE_PHRASES,
    EXPERIENCE_CLAIM_PHRASES,
    HYPE_PHRASES,
    IMAGE_SHOT_ROTATION,
    visual_style_for,
)
from app.llm.trends.similarity import jaccard_similarity
from app.modules.draft.content_validation import (
    _paragraphs_per_section,
    _prose_paragraphs,
    first_substantive_paragraph,
)
from app.modules.draft.quality import (
    EMPHASIS_TAG,
    SENTENCE_SPLIT,
    TOKEN,
    _cliche_hits,
    _repeated_ngram_rate,
    _repeated_sentence_count,
    body_char_count,
    connective_openings,
    normalize_for_match,
)
from app.modules.trend.topic_scoring import CLICKBAIT_MARKERS
from app.shared import FinalPost, TopicCandidate

# 목차처럼 반복되면 글이 기계적으로 읽히는 연결어. PDF 5-2의 3번 규칙이 지목한 것 + 이미
# 프롬프트가 금지한 것(prompts.py의 HUMAN_RHYTHM_RULES).
CONNECTIVES: tuple[str, ...] = (
    "먼저",
    "다음으로",
    "또한",
    "마지막으로",
    "결론적으로",
    "정리하자면",
    "한마디로 정리하면",
    "그렇다면",
    "자, 이제",
    "결국",
)

# 마크다운 강조. quality.EMPHASIS_TAG는 HTML(<strong>/<mark>)만 보므로 마크다운 본문에는
# 걸리지 않는다 — 원고가 반환하는 것은 markdownContent라서 여기서 따로 센다.
MARKDOWN_EMPHASIS = re.compile(r"\*\*[^*\n]+\*\*|==[^=\n]+==")
LIST_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
HEADING_LINE = re.compile(r"^\s*(#{1,6})\s+(.*)$")

# 수치 주장. '3가지'·'2026년'처럼 구조·연도 표기는 사실 주장이 아니라서 제외한다.
NUMERIC_CLAIM = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|퍼센트|배|만원|원|분|초|시간|도|kg|km|g|ml|L|명|건|개월|년차)")
STRUCTURAL_NUMBER = re.compile(r"\d+\s*(?:가지|단계|번째|번|개(?!월))")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT.split(text or "") if part.strip()]


def _headings(markdown: str, level: int = 2) -> list[str]:
    found: list[str] = []
    for line in (markdown or "").splitlines():
        match = HEADING_LINE.match(line)
        if match and len(match.group(1)) == level:
            found.append(match.group(2).strip())
    return found


def _noun_set(text: str) -> set[str]:
    """어절 단위 집합. 형태소 분석은 하지 않는다.

    트렌드 쪽 `keyword_tokens`를 쓰지 않는 이유: 그 함수는 짧은 검색어를 위해 만들어져
    저품질 어휘를 걸러내므로, 본문 문단에 쓰면 남는 토큰이 거의 없어 중복률이 늘 0이 된다.
    여기서는 '겹치는 말이 얼마나 되나'만 보면 되므로 두 글자 이상 어절을 그대로 쓴다.
    """
    return {token.lower() for token in TOKEN.findall(text or "") if len(token) >= 2}


# ---------------------------------------------------------------------------
# 2-2 제목 지표
# ---------------------------------------------------------------------------

TITLE_MIN_LEN = 25
TITLE_MAX_LEN = 45


@dataclass
class TitleMetrics:
    candidate_count: int
    lengths: list[int]
    length_compliance: float
    hook_type_distribution: dict[str, int]
    hook_strength_distribution: dict[str, int]
    title_type_distribution: dict[str, int]
    mean_pairwise_noun_overlap: float
    max_pairwise_noun_overlap: float
    duplicate_start_patterns: int
    same_angle_pairs: int
    max_similarity_to_excluded: float
    titles_with_unsourced_numbers: int
    clickbait_titles: int
    # 재생성 사례에서만 채운다: 직전 배치와 같은 (hookType, titleType) 조합 수.
    repeated_hook_title_combos: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def title_metrics(
    candidates: list[TopicCandidate],
    *,
    exclude_titles: list[str] | None = None,
    has_reference_material: bool = False,
    previous_combos: set[tuple[str, str]] | None = None,
) -> TitleMetrics:
    titles = [candidate.title for candidate in candidates]
    lengths = [len(title) for title in titles]
    compliant = sum(1 for length in lengths if TITLE_MIN_LEN <= length <= TITLE_MAX_LEN)

    hooks: dict[str, int] = {}
    strengths: dict[str, int] = {}
    types: dict[str, int] = {}
    for candidate in candidates:
        hook = candidate.hook_type.value if candidate.hook_type else "(없음)"
        strength = candidate.hook_strength.value if candidate.hook_strength else "(없음)"
        hooks[hook] = hooks.get(hook, 0) + 1
        strengths[strength] = strengths.get(strength, 0) + 1
        types[candidate.description] = types.get(candidate.description, 0) + 1

    overlaps: list[float] = []
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            overlaps.append(jaccard_similarity(_noun_set(titles[i]), _noun_set(titles[j])))

    starts: dict[str, int] = {}
    for title in titles:
        prefix = " ".join(title.split()[:2])
        starts[prefix] = starts.get(prefix, 0) + 1
    duplicate_starts = sum(count - 1 for count in starts.values() if count > 1)

    angles: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        angle = (
            candidate.hook_type.value if candidate.hook_type else "",
            candidate.description or "",
        )
        angles[angle] = angles.get(angle, 0) + 1
    same_angle_pairs = sum(count - 1 for count in angles.values() if count > 1)

    excluded = exclude_titles or []
    max_excluded = 0.0
    for title in titles:
        for old in excluded:
            max_excluded = max(
                max_excluded, jaccard_similarity(_noun_set(title), _noun_set(old))
            )

    unsourced_numbers = 0
    if not has_reference_material:
        for title in titles:
            stripped = STRUCTURAL_NUMBER.sub("", title)
            if re.search(r"\d", stripped):
                unsourced_numbers += 1

    clickbait = sum(
        1 for title in titles if any(marker in title for marker in CLICKBAIT_MARKERS)
    )

    repeated_combos = 0
    if previous_combos:
        for candidate in candidates:
            combo = (
                candidate.hook_type.value if candidate.hook_type else "",
                candidate.description or "",
            )
            if combo in previous_combos:
                repeated_combos += 1

    return TitleMetrics(
        candidate_count=len(candidates),
        lengths=lengths,
        length_compliance=round(compliant / len(titles), 4) if titles else 0.0,
        hook_type_distribution=hooks,
        hook_strength_distribution=strengths,
        title_type_distribution=types,
        mean_pairwise_noun_overlap=round(statistics.fmean(overlaps), 4) if overlaps else 0.0,
        max_pairwise_noun_overlap=round(max(overlaps), 4) if overlaps else 0.0,
        duplicate_start_patterns=duplicate_starts,
        same_angle_pairs=same_angle_pairs,
        max_similarity_to_excluded=round(max_excluded, 4),
        titles_with_unsourced_numbers=unsourced_numbers,
        clickbait_titles=clickbait,
        repeated_hook_title_combos=repeated_combos,
    )


# ---------------------------------------------------------------------------
# 2-3 원고 지표
# ---------------------------------------------------------------------------


@dataclass
class DraftMetrics:
    # 분량
    body_chars: int
    target_min: int
    target_max: int
    within_target: bool
    delta_to_target: int
    # 구조
    h2_count: int
    paragraph_count: int
    paragraph_chars_min: int
    paragraph_chars_median: float
    paragraph_chars_max: int
    paragraph_length_cv: float
    uniform_paragraph_length: bool
    identical_paragraphs_per_section: bool
    paragraphs_per_section: list[int]
    uniform_h2_grammar: bool
    h2_endings: dict[str, int]
    # 반복·상투
    repeated_ngram_rate: float
    repeated_ngram_count: int
    repeated_sentence_count: int
    cliche_hits: list[str]
    intro_cliche_hits: list[str]
    connective_counts: dict[str, int]
    max_connective_repeats: int
    max_same_sentence_ending_run: int
    title_first_paragraph_overlap: float
    conclusion_body_overlap: float
    # 표현
    emphasis_count: int
    emphasis_ratio: float
    list_line_ratio: float
    hype_hits: list[str]
    experience_claim_hits: list[str]
    unsupported_numeric_claims: list[str]
    # SEO
    seo_primary: str | None
    seo_primary_count: int
    seo_primary_per_1000_chars: float
    seo_primary_in_title: bool
    seo_primary_in_first_paragraph: bool
    seo_secondary_used: int
    seo_secondary_total: int
    seo_avoid_violations: list[str]
    # 기존 검사 결과
    quality_ok: bool
    quality_problems: list[str] = field(default_factory=list)
    quality_warnings: list[str] = field(default_factory=list)
    validation_failures: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    # 실행 정보(실측 모드에서만 채워진다)
    revision_attempts: int = 0
    stop_reasons: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    hashtag_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _sentence_ending_run(markdown: str) -> int:
    endings = [sentence[-3:] for sentence in _sentences(markdown) if len(sentence) >= 3]
    best = current = 0
    previous: str | None = None
    for ending in endings:
        if ending == previous:
            current += 1
        else:
            current = 1
            previous = ending
        best = max(best, current)
    return best


def _uniform_h2_grammar(headings: list[str]) -> tuple[bool, dict[str, int]]:
    """소제목이 모두 같은 문법 패턴인가. 끝 두 글자를 패턴 대용으로 쓴다 — 한국어 소제목은
    '~하는 방법'·'~인 이유'처럼 어미가 형태를 결정한다."""
    endings: dict[str, int] = {}
    for heading in headings:
        key = heading.strip()[-2:] if len(heading.strip()) >= 2 else heading.strip()
        endings[key] = endings.get(key, 0) + 1
    if len(headings) < 3:
        return False, endings
    return max(endings.values()) == len(headings), endings


def draft_metrics(
    post: FinalPost,
    *,
    target_min: int,
    target_max: int,
    seo_plan=None,
    reference_text: str = "",
    quality_report=None,
    validation_result=None,
    revision_attempts: int = 0,
) -> DraftMetrics:
    markdown = post.markdown_content or post.body or ""
    chars = body_char_count(post.body or markdown)

    prose = _prose_paragraphs(markdown)
    para_lengths = [len(paragraph) for paragraph in prose] or [0]
    mean_length = statistics.fmean(para_lengths)
    spread = (max(para_lengths) - min(para_lengths)) / mean_length if mean_length else 0.0

    headings = _headings(markdown, level=2)
    uniform_grammar, endings = _uniform_h2_grammar(headings)
    per_section = _paragraphs_per_section(markdown)

    rate, duplicate_count = _repeated_ngram_rate(markdown)
    intro = first_substantive_paragraph(markdown)

    # 운용 검사와 같은 자로 잰다(quality.connective_openings). 본문 전체에서 낱말을 세면
    # '바닥재를 먼저 확인한다' 같은 정상 문장까지 목차형 연결어로 잡힌다.
    connectives = {
        connective: count
        for connective in CONNECTIVES
        if (count := connective_openings(markdown, connective)) > 0
    }

    title_tokens = _noun_set(post.title)
    intro_overlap = jaccard_similarity(title_tokens, _noun_set(intro)) if intro else 0.0

    conclusion = prose[-1] if prose else ""
    heading_tokens: set[str] = set()
    for heading in headings:
        heading_tokens |= _noun_set(heading)
    conclusion_overlap = (
        jaccard_similarity(_noun_set(conclusion), heading_tokens) if conclusion else 0.0
    )

    sentences = _sentences(markdown)
    emphasis = len(MARKDOWN_EMPHASIS.findall(markdown)) + len(EMPHASIS_TAG.findall(markdown))

    lines = [line for line in markdown.splitlines() if line.strip()]
    list_lines = sum(1 for line in lines if LIST_LINE.match(line))

    reference_normalized = normalize_for_match(reference_text)
    unsupported: list[str] = []
    for match in NUMERIC_CLAIM.finditer(markdown):
        claim = match.group(0).strip()
        if STRUCTURAL_NUMBER.fullmatch(claim):
            continue
        if reference_normalized and normalize_for_match(claim) in reference_normalized:
            continue
        digits = re.sub(r"\D", "", claim)
        if digits and reference_normalized and digits in reference_normalized:
            continue
        unsupported.append(claim)

    primary = getattr(seo_plan, "primary", None) if seo_plan else None
    secondary = list(getattr(seo_plan, "secondary", []) or []) if seo_plan else []
    avoid = list(getattr(seo_plan, "avoid", []) or []) if seo_plan else []
    body_normalized = normalize_for_match(markdown)
    primary_count = (
        body_normalized.count(normalize_for_match(primary)) if primary else 0
    )
    secondary_used = sum(
        1 for keyword in secondary if normalize_for_match(keyword) in body_normalized
    )
    avoid_violations = [
        keyword for keyword in avoid if normalize_for_match(keyword) in body_normalized
    ]

    return DraftMetrics(
        body_chars=chars,
        target_min=target_min,
        target_max=target_max,
        within_target=target_min <= chars <= target_max,
        delta_to_target=(
            0 if target_min <= chars <= target_max
            else (chars - target_max if chars > target_max else chars - target_min)
        ),
        h2_count=len(headings),
        paragraph_count=len(prose),
        paragraph_chars_min=min(para_lengths),
        paragraph_chars_median=round(statistics.median(para_lengths), 1),
        paragraph_chars_max=max(para_lengths),
        paragraph_length_cv=(
            round(statistics.pstdev(para_lengths) / mean_length, 4)
            if mean_length and len(para_lengths) > 1
            else 0.0
        ),
        uniform_paragraph_length=len(prose) >= 6 and spread < 0.4,
        identical_paragraphs_per_section=(
            len(per_section) >= 3 and len(set(per_section)) == 1
        ),
        paragraphs_per_section=per_section,
        uniform_h2_grammar=uniform_grammar,
        h2_endings=endings,
        repeated_ngram_rate=round(rate, 4),
        repeated_ngram_count=duplicate_count,
        repeated_sentence_count=_repeated_sentence_count(markdown),
        cliche_hits=_cliche_hits(markdown),
        intro_cliche_hits=[phrase for phrase in CLICHE_PHRASES if phrase in intro],
        connective_counts=connectives,
        max_connective_repeats=max(connectives.values()) if connectives else 0,
        max_same_sentence_ending_run=_sentence_ending_run(markdown),
        title_first_paragraph_overlap=round(intro_overlap, 4),
        conclusion_body_overlap=round(conclusion_overlap, 4),
        emphasis_count=emphasis,
        emphasis_ratio=round(emphasis / len(sentences), 4) if sentences else 0.0,
        list_line_ratio=round(list_lines / len(lines), 4) if lines else 0.0,
        hype_hits=[phrase for phrase in HYPE_PHRASES if phrase in markdown],
        experience_claim_hits=[
            phrase for phrase in EXPERIENCE_CLAIM_PHRASES if phrase in markdown
        ],
        unsupported_numeric_claims=unsupported,
        seo_primary=primary,
        seo_primary_count=primary_count,
        seo_primary_per_1000_chars=round(primary_count / (chars / 1000), 3) if chars else 0.0,
        seo_primary_in_title=(
            bool(primary) and normalize_for_match(primary) in normalize_for_match(post.title)
        ),
        seo_primary_in_first_paragraph=(
            bool(primary) and normalize_for_match(primary) in normalize_for_match(intro)
        ),
        seo_secondary_used=secondary_used,
        seo_secondary_total=len(secondary),
        seo_avoid_violations=avoid_violations,
        quality_ok=bool(getattr(quality_report, "ok", False)),
        quality_problems=list(getattr(quality_report, "problems", []) or []),
        quality_warnings=list(getattr(quality_report, "warnings", []) or []),
        validation_failures=_validation_names(validation_result, "FAIL"),
        validation_warnings=_validation_names(validation_result, "WARN"),
        revision_attempts=revision_attempts,
        hashtag_count=len(post.hashtags or []),
    )


def _validation_names(result, status: str) -> list[str]:
    checks = getattr(result, "checks", None) or []
    names: list[str] = []
    for check in checks:
        value = getattr(check, "status", None)
        text = getattr(value, "value", value)
        if str(text) == status:
            names.append(str(getattr(check, "name", "?")))
    return names


# ---------------------------------------------------------------------------
# 2-4 시각자료 계획 지표
# ---------------------------------------------------------------------------


@dataclass
class VisualPlanMetrics:
    card_type_distribution: dict[str, int]
    photo_role_distribution: dict[str, int]
    planned_visual_types: dict[str, int]
    charts_without_numeric_material: int
    all_visuals_refused_though_budgeted: bool
    rendered_budget: int
    shot_per_slot: list[str]
    palette_per_slot: list[str]
    consecutive_same_shot: int
    necessity_scores: list[float]

    def as_dict(self) -> dict:
        return asdict(self)


CHART_TYPES = frozenset({"BAR_CHART", "LINE_CHART", "PIE_CHART"})


def visual_plan_metrics(
    *,
    card_plan=None,
    content_plan=None,
    rendered_budget: int = 0,
    has_numeric_material: bool = False,
    post_id: str = "post",
    slot_count: int = 3,
) -> VisualPlanMetrics:
    cards = list(getattr(card_plan, "cards", []) or []) if card_plan else []
    card_types: dict[str, int] = {}
    roles: dict[str, int] = {}
    scores: list[float] = []
    for card in cards:
        card_type = _enum_text(getattr(card, "card_type", None))
        role = _enum_text(getattr(card, "photo_role", None))
        card_types[card_type] = card_types.get(card_type, 0) + 1
        roles[role] = roles.get(role, 0) + 1
        score = getattr(card, "necessity_score", None)
        if score is not None:
            scores.append(float(score))

    visual_types: dict[str, int] = {}
    charts_without_data = 0
    for section in list(getattr(content_plan, "sections", []) or []):
        visual = _enum_text(getattr(section, "visual_type", None))
        if visual and visual != "None":
            visual_types[visual] = visual_types.get(visual, 0) + 1
        if visual in CHART_TYPES and not has_numeric_material:
            charts_without_data += 1

    planned_rendered = sum(
        count for visual, count in visual_types.items() if visual != "NONE"
    )

    shots = [
        IMAGE_SHOT_ROTATION[index % len(IMAGE_SHOT_ROTATION)] for index in range(slot_count)
    ]
    palettes = [visual_style_for(post_id) for _ in range(slot_count)]
    consecutive = 0
    for index in range(1, len(shots)):
        if shots[index] == shots[index - 1]:
            consecutive += 1

    return VisualPlanMetrics(
        card_type_distribution=card_types,
        photo_role_distribution=roles,
        planned_visual_types=visual_types,
        charts_without_numeric_material=charts_without_data,
        all_visuals_refused_though_budgeted=rendered_budget > 0 and planned_rendered == 0,
        rendered_budget=rendered_budget,
        shot_per_slot=[shot[:40] for shot in shots],
        palette_per_slot=[palette[:40] for palette in palettes],
        consecutive_same_shot=consecutive,
        necessity_scores=scores,
    )


def _enum_text(value) -> str:
    if value is None:
        return "(없음)"
    return str(getattr(value, "value", value))


def rotation_determinism() -> dict:
    """API 없이 확인할 수 있는 것: 같은 post_id는 같은 팔레트, 연속 슬롯은 다른 구도."""
    samples = [f"post_{index}" for index in range(24)]
    palettes = {sample: visual_style_for(sample) for sample in samples}
    stable = all(visual_style_for(sample) == palettes[sample] for sample in samples)
    distinct_palettes = len(set(palettes.values()))
    shots = [IMAGE_SHOT_ROTATION[index % len(IMAGE_SHOT_ROTATION)] for index in range(12)]
    neighbours_differ = all(shots[i] != shots[i - 1] for i in range(1, len(shots)))
    wraps = shots[len(IMAGE_SHOT_ROTATION)] == shots[0]
    return {
        "palette_stable_per_post": stable,
        "distinct_palettes_over_24_posts": distinct_palettes,
        "palette_pool_size": len(set(palettes.values())),
        "shot_pool_size": len(IMAGE_SHOT_ROTATION),
        "neighbour_shots_differ": neighbours_differ,
        "rotation_wraps_to_start": wraps,
    }
