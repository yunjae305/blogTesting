"""제목 후보 루브릭 채점.

'추천' 배지를 index==0 같은 임의 기준이 아니라 **설명 가능한 점수**로 고른다.

루브릭(트렌드 선택 시, 합 100):
- 소재 관련성 30
- 트렌드 반영 25
- 글 목적 부합 20
- 대상 독자 관심 15
- 제목 완성도(길이·가독성·낚시 여부) 10

트렌드를 선택하지 않으면 트렌드 항(25)을 빼고 나머지 네 항(합 75)을 100점으로 재정규화한다.

관련성·목적·독자 관심은 의미 판단이라 LLM 배치 평가(있으면)가 채우고, 없으면 규칙 기반 근사값을
쓴다. 제목 완성도와 소재·트렌드 포함 여부는 규칙으로 결정한다(결정적). 이 파일에는 네트워크·모델
호출이 없다 — 순수 함수라 그대로 단위 테스트한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.llm.trends.similarity import jaccard_similarity, keyword_tokens
from app.llm.trends.text import noun_tokens
from app.shared import TopicCandidate

# 루브릭 가중치. 합은 100.
RUBRIC_WITH_TREND: dict[str, float] = {
    "relevance": 30.0,
    "trend": 25.0,
    "purpose": 20.0,
    "audience": 15.0,
    "quality": 10.0,
}
# 트렌드 미선택: 트렌드 항을 빼고 남은 네 항(합 75)을 100으로 재정규화.
_WITHOUT_TREND_BASE: dict[str, float] = {
    "relevance": 30.0,
    "purpose": 20.0,
    "audience": 15.0,
    "quality": 10.0,
}
RUBRIC_WITHOUT_TREND: dict[str, float] = {
    key: round(value * 100.0 / sum(_WITHOUT_TREND_BASE.values()), 4)
    for key, value in _WITHOUT_TREND_BASE.items()
}

CRITERION_LABELS: dict[str, str] = {
    "relevance": "소재 관련성",
    "trend": "트렌드 반영",
    "purpose": "글 목적 부합",
    "audience": "대상 독자 관심",
    "quality": "제목 완성도",
}

# 과장·충격·무조건 류 낚시 표현. 있으면 완성도(품질)를 감점한다. 뒤쪽 묶음은 후킹
# 스펙(§9)이 실제 근거 없이는 금지한 문구들 — 근거를 제목만으로 확인할 수 없어 항상 감점한다.
# (조건부로 허용되는 모호 표현 '이것만 알면' 류는 여기 넣지 않는다. 소재 포함 여부를 보는
#  has_subject 게이트가 이미 걸러, 정상 제목까지 오검하지 않게 한다.)
CLICKBAIT_MARKERS: tuple[str, ...] = (
    "충격",
    "무조건",
    "대박",
    "소름",
    "미쳤",
    "미친",
    "실화",
    "경악",
    "발칵",
    "난리",
    "역대급",
    "레전드",
    "완전정복",
    "필독",
    "긴급",
    # 후킹 스펙 §9: 근거 없는 과장·공포 문구.
    "상위 1%",
    "모두가 하고",
    "전문가도 놀",
    "아무도 알려주지 않",
    "아무도 안 알려",
    "지금 안 보면",
    "오늘이 마지막",
    "인생이 바뀝",
    "인생이 완전히",
    "100% 효과",
)
CLICKBAIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+\s*%"),  # 출처 없는 비율(80%)
    re.compile(r"\d+\s*명\s*중"),  # 10명 중 7명
    re.compile(r"[!?]{2,}"),  # !!, ?!
)

IDEAL_MAX_LEN = 45  # 생성 프롬프트가 요구하는 상한
HARD_MAX_LEN = 60  # 이보다 길면 확연히 감점
MIN_LEN = 10
NEAR_DUP_THRESHOLD = 0.7  # 명사 집합이 이만큼 겹치면 '단어 순서만 바꾼' 중복으로 본다
NEAR_DUP_PENALTY = 0.6  # 중복 후속 제목의 총점 배수
DEFAULT_SUBJECTIVE = 60.0  # LLM 평가가 없을 때 목적·독자의 중립값

_NON_WORD = re.compile(r"[^0-9a-z가-힣]")


@dataclass(frozen=True)
class TitleJudgmentScore:
    """제목 하나에 대한 의미 판단(0-100). LLM 배치 평가기가 채운다."""

    relevance: float
    trend_reflection: float
    purpose_match: float
    audience_interest: float
    reason: str | None = None


@dataclass(frozen=True)
class ScoreContext:
    subject_tokens: frozenset[str]
    trend_keyword: str | None
    purpose: tuple[str, ...]
    audience: str | None

    @property
    def has_trend(self) -> bool:
        return bool(self.trend_keyword and self.trend_keyword.strip())


@dataclass(frozen=True)
class TitleRuleResult:
    quality: float  # 0-100
    has_subject: bool
    has_trend: bool
    is_clickbait: bool
    length: int


def build_context(
    *,
    topic: str,
    subject: str | None,
    purpose: list[str] | None,
    audience: str | None,
    trend_keyword: str | None,
) -> ScoreContext:
    """소재·주제에서 소재 토큰을 뽑아 채점 문맥을 만든다."""
    tokens: set[str] = set(keyword_tokens(topic))
    if subject:
        tokens |= set(keyword_tokens(subject))
    return ScoreContext(
        subject_tokens=frozenset(tokens),
        trend_keyword=trend_keyword,
        purpose=tuple(purpose or ()),
        audience=audience,
    )


def _compact(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


def title_contains_trend(title: str, keyword: str | None) -> bool:
    if not keyword:
        return False
    return _compact(keyword) in _compact(title)


def title_contains_subject(title: str, subject_tokens: frozenset[str]) -> bool:
    compact = _compact(title)
    return any(token and token in compact for token in subject_tokens)


def evaluate_rules(title: str, ctx: ScoreContext) -> TitleRuleResult:
    """결정적 규칙: 길이·낚시로 완성도(0-100)를 매기고, 소재·트렌드 포함 여부를 본다."""
    text = title.strip()
    length = len(text)
    quality = 100.0

    if length > HARD_MAX_LEN:
        quality -= 50.0
    elif length > IDEAL_MAX_LEN:
        quality -= (length - IDEAL_MAX_LEN) * 3.0
    if length < MIN_LEN:
        quality -= 25.0

    hits = sum(1 for marker in CLICKBAIT_MARKERS if marker in text)
    hits += sum(1 for pattern in CLICKBAIT_PATTERNS if pattern.search(text))
    if hits:
        quality -= min(60.0, hits * 22.0)

    return TitleRuleResult(
        quality=max(0.0, min(100.0, quality)),
        has_subject=title_contains_subject(text, ctx.subject_tokens),
        has_trend=title_contains_trend(text, ctx.trend_keyword) if ctx.has_trend else False,
        is_clickbait=hits > 0,
        length=length,
    )


def near_duplicate_indices(titles: list[str]) -> set[int]:
    """앞선 제목과 명사 집합이 거의 같은(단어 순서만 바꾼) 후속 제목의 인덱스.

    조사·어미가 붙은 제목 문장을 비교하므로 명사 어간(noun_tokens)으로 집합을 만든다 —
    '콘텐츠에'와 '콘텐츠', 'AIONA로'와 'AIONA'가 같은 토큰이 되어 순서만 바꾼 변형이 잡힌다.
    """
    token_sets = [set(noun_tokens(title)) for title in titles]
    duplicates: set[int] = set()
    for i in range(len(titles)):
        for j in range(i):
            if (
                token_sets[i]
                and token_sets[j]
                and jaccard_similarity(token_sets[i], token_sets[j]) >= NEAR_DUP_THRESHOLD
            ):
                duplicates.add(i)
                break
    return duplicates


def _subjective_scores(
    rule: TitleRuleResult, ctx: ScoreContext, judgment: TitleJudgmentScore | None
) -> dict[str, float]:
    """의미 판단 항(관련성·트렌드·목적·독자) + 완성도를 0-100으로. LLM 평가가 있으면 그 값을,
    없으면 규칙 근사값을 쓰고, 규칙 감점(소재·트렌드 누락)을 반영한다."""
    if judgment is not None:
        relevance = judgment.relevance
        trend = judgment.trend_reflection
        purpose = judgment.purpose_match
        audience = judgment.audience_interest
    else:
        relevance = 75.0 if rule.has_subject else 35.0
        trend = 80.0 if rule.has_trend else 20.0
        purpose = DEFAULT_SUBJECTIVE
        audience = DEFAULT_SUBJECTIVE

    # 규칙 감점: 소재가 빠진 제목은 관련성을, 트렌드가 빠진 제목은 트렌드 반영을 상한으로 누른다.
    if not rule.has_subject:
        relevance = min(relevance, 40.0)
    if ctx.has_trend and not rule.has_trend:
        trend = min(trend, 25.0)

    return {
        "relevance": relevance,
        "trend": trend,
        "purpose": purpose,
        "audience": audience,
        "quality": rule.quality,
    }


def _templated_reason(subs: dict[str, float], weights: dict[str, float], recommended: bool) -> str:
    """LLM 평가가 없을 때의 근거 한 줄. 기여도(점수×가중치) 상위 두 항으로 만든다."""
    ranked = sorted(weights, key=lambda key: subs[key] / 100.0 * weights[key], reverse=True)
    labels = [CRITERION_LABELS[key] for key in ranked[:2]]
    strengths = "·".join(labels)
    if recommended:
        return f"{strengths}에서 가장 높아 추천합니다."
    return f"{strengths}이(가) 상대적으로 강한 제목입니다."


def score_titles(
    candidates: list[TopicCandidate],
    ctx: ScoreContext,
    judgments: dict[str, TitleJudgmentScore] | None = None,
) -> list[TopicCandidate]:
    """후보마다 루브릭 총점을 매기고, 최고점 하나에 추천을 표시하며 근거를 붙인다.

    반환 순서는 입력 순서를 그대로 둔다(생성이 후킹 유형별로 배치한 다양성 순서). 점수는 정렬이
    아니라 추천 선정과 표시에 쓴다.
    """
    if not candidates:
        return []

    weights = RUBRIC_WITH_TREND if ctx.has_trend else RUBRIC_WITHOUT_TREND
    judgments = judgments or {}
    duplicates = near_duplicate_indices([candidate.title for candidate in candidates])

    totals: list[float] = []
    per_title: list[dict[str, float]] = []
    for index, candidate in enumerate(candidates):
        rule = evaluate_rules(candidate.title, ctx)
        subs = _subjective_scores(rule, ctx, judgments.get(candidate.title))
        total = sum(subs[key] / 100.0 * weight for key, weight in weights.items())
        if index in duplicates:
            total *= NEAR_DUP_PENALTY  # 단어 순서만 바꾼 중복은 강하게 감점
        per_title.append(subs)
        totals.append(round(max(0.0, min(100.0, total)), 1))

    # 추천 = 최고점(동점이면 앞선 순서 = 다양성 배치상 앞 후킹).
    best_index = max(range(len(candidates)), key=lambda i: (totals[i], -i))

    result: list[TopicCandidate] = []
    for index, candidate in enumerate(candidates):
        recommended = index == best_index
        judgment = judgments.get(candidate.title)
        reason = (
            judgment.reason
            if judgment is not None and judgment.reason
            else _templated_reason(per_title[index], weights, recommended)
        )
        result.append(
            candidate.model_copy(
                update={"recommended": recommended, "score": totals[index], "reason": reason}
            )
        )
    return result
