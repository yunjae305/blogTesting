"""Live provider 어댑터.

원본의 createOpenAiWebSearchAnalyzer는 일부러 포팅하지 않았다: factory가 한 번도 연결하지
않아 죽은 코드였다.
"""

import asyncio
import base64
import binascii
import hashlib
import logging
import random
import time
from dataclasses import dataclass
from app.shared.format import now_iso as _now
from typing import Any

import httpx

from app.shared import perf
from app.shared.image_bytes import UnsafeImageError
from app.shared.ids import short
from app.shared.reference_url import is_public_reference_url

from app.shared import (
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    DraftGenerationInput,
    DraftGenerationResult,
    FinalPost,
    GeneratedPostImage,
    IntentCandidate,
    IntentValidationResult,
    ReferenceMaterial,
    SearchSource,
    WebPhoto,
    WebSearchAnalysisInput,
)

from . import image_origin, imaging, prompts
from .http import shared_client
from .naver_blog import to_mobile_url
from .source_quality import drop_blocked_sources
from .keyword_naturalization import primary_raw_keyword
from .contracts import (
    FeatureBrief,
    KeywordJudgment,
    KeywordRelevanceInput,
    OnResearchCollected,
    OnResearchNote,
    PostImageGenerationInput,
    SiteReadInput,
    TitleEvaluationInput,
    TitleJudgment,
    TopicGenerationInput,
    TopicRecommendationResult,
)
from .extractors import (
    GeminiUrlContextResult,
    extract_anthropic_text,
    extract_anthropic_tool_input,
    extract_gemini_interaction_sources,
    extract_gemini_interaction_text,
    extract_gemini_url_context_results,
    extract_gemini_text,
    extract_openai_image_base64,
    extract_openai_text,
)
from .parsing import (
    LiveAdapterError,
    ProviderContextExceededError,
    ProviderEmptyResponseError,
    ProviderOverloadedError,
    ProviderRefusedError,
    ProviderTruncatedError,
    card_plan_from_json,
    content_plan_from_json,
    dedupe_sources,
    editorial_style_plan_from_json,
    final_review_checks_from_json,
    final_review_issues_from_json,
    final_review_overall_from_json,
    polish_edits_from_json,
    reference_evidence_profile_from_json,
    seo_keyword_plan_from_json,
    title_plan_from_json,
    extract_json_object,
    final_post_from_json,
    planned_visuals_from_json,
    sources_from_indexes,
    sources_value,
    string_array,
    string_value,
    strip_internal_notes,
)
from .provider_config import RoleConfig
from .schemas import (
    CARD_PLAN_SCHEMA,
    CONTENT_PLAN_SCHEMA,
    EDITORIAL_STYLE_PLAN_SCHEMA,
    CRITIQUE_SCHEMA,
    FINAL_REVIEW_SCHEMA,
    INTEGRATION_SCHEMA,
    POLISH_SCHEMA,
    REFERENCE_EVIDENCE_SCHEMA,
    SEO_KEYWORD_PLAN_SCHEMA,
    TITLE_PLAN_SCHEMA,
    RELATION_TYPES,
    RELEVANCE_SCHEMA,
    DRAFT_SCHEMA,
    GEMINI_FEATURE_BRIEF_SCHEMA,
    GEMINI_INTENT_SCHEMA,
    TITLE_EVALUATION_SCHEMA,
    TITLE_HOOK_STRENGTHS,
    TITLE_HOOK_TYPES,
    TOPIC_CANDIDATE_COUNT,
    TOPIC_SCHEMA,
    TREND_CATEGORIES,
    WEB_PHOTO_GATE_SCHEMA,
)
from app.shared import (
    ContentPlan,
    FinalReviewReport,
    EditorialStylePlan,
    ReferenceEvidenceProfile,
    RelationType,
    SeoKeywordPlan,
    TitlePlan,
    TitleHookStrength,
    TitleHookType,
    TopicCandidate,
)

logger = logging.getLogger(__name__)

DRAFT_TOOL_NAME = "return_blog_draft"
CONTENT_PLAN_TOOL_NAME = "return_content_plan"
SEO_KEYWORD_PLAN_TOOL_NAME = "return_seo_keyword_plan"
TITLE_PLAN_TOOL_NAME = "return_title_plan"
CARD_PLAN_TOOL_NAME = "return_card_plan"
FINAL_REVIEW_TOOL_NAME = "return_final_review"
CRITIQUE_TOOL_NAME = "return_article_critique"
INTEGRATION_TOOL_NAME = "return_integrated_article"
POLISH_TOOL_NAME = "return_polish_edits"
REFERENCE_EVIDENCE_TOOL_NAME = "return_reference_evidence"
EDITORIAL_STYLE_TOOL_NAME = "return_editorial_style_plan"
TOPIC_TOOL_NAME = "return_title_candidates"
RELEVANCE_TOOL_NAME = "return_keyword_relevance"
TITLE_EVALUATION_TOOL_NAME = "return_title_scores"

# 대표 이미지는 피드에서 글 전체를 결정하므로 정사각 원본을 high로 한 번 만들고, 본문은
# 최종 비율에 가까운 원본을 medium으로 만든다. gpt-image-2는 두 변이 16의 배수이고
# 655,360픽셀 이상인 사용자 지정 크기를 지원한다(1200×688 = 825,600픽셀).
COVER_IMAGE_SIZE = "1024x1024"
COVER_IMAGE_QUALITY = "high"
BODY_IMAGE_SIZE = "1200x688"
BODY_IMAGE_QUALITY = "medium"
LEGACY_LANDSCAPE_IMAGE_SIZE = "1536x1024"

# 일시적인 제공자 오류는 재시도로 넘긴다. gemini·anthropic·openai 모두 수요가 몰리면
# 5xx나 429(레이트리밋)를 내는데(예: gemini "currently experiencing high demand", anthropic
# 529 overloaded), 이건 결정적 실패가 아니라 잠깐 기다렸다 다시 부르면 대개 성공한다.
# 재시도 없이 한 번에 실패로 처리하면 검증(M3)·원고(M4)·이미지(M5)가 모두 같은 _post_json을
# 쓰므로, 일시적 혼잡 한 번에 세 단계가 통째로 무너진다. 400·401·404 같은 결정적 오류
# (잘못된 요청·키·모델)는 재시도해도 소용없으므로 아래 목록에서 뺀다.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
MAX_REQUEST_ATTEMPTS = 4
_RETRY_BASE_DELAY = 1.5  # 초. 지수 백오프의 기준(1.5 → 3 → 6 …).
_RETRY_MAX_DELAY = 20.0  # 초. 한 번의 대기가 이보다 길어지지 않게 한다.


class ProviderRequestError(LiveAdapterError):
    """재시도 대상이 아닌 provider 4xx. 이미지 규격 폴백이 구조적으로 판별할 수 있다."""

    def __init__(self, status_code: int, detail: str, payload: Any):
        super().__init__(f"provider request failed with {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.payload = payload

# ---------------------------------------------------------------------------
# Anthropic 요청 조립 (Claude Opus 5)
#
# 왜 중앙화하는가: 요청 옵션이 호출부마다 복제되면 모델을 갈아탈 때 제거된 파라미터가 일부
# 경로에만 남는다. 실제로 그런 상태였다 — temperature가 11곳에 흩어져 있었고, Opus 5는 그런
# 요청을 하나하나 400으로 거절한다(실측 응답: "`temperature` is deprecated for this model").
# 한 곳에서 조립하면 effort와 thinking의 허용 조합도 한 번만 검증하면 되고, stop_reason 검사도
# 모든 호출이 같은 문을 지나게 된다.
# ---------------------------------------------------------------------------
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
# thinking을 끈 요청에 허용되는 effort 상한. 실측: disabled + xhigh는 400을 받는다
# ("output_config.effort 'xhigh' is not supported when thinking is disabled on this model").
EFFORT_ALLOWED_WITH_THINKING_DISABLED = frozenset({"low", "medium", "high"})

THINKING_ADAPTIVE = "adaptive"
THINKING_DISABLED = "disabled"


@dataclass(frozen=True)
class StageBudget:
    """한 단계가 쓰는 계산량·출력 예산.

    effort는 창의성 조절값이 **아니다** — 모델이 문제 해결에 쓰는 계산량이다. 제목의 다양성은
    후보 슬롯과 코드 회전이 만들고, 채점의 일관성은 고정 절차·스키마·캐시가 만들고, 원고 분량은
    프롬프트와 품질 검사가 제어한다.

    max_tokens는 Opus 5에서 **thinking과 응답 텍스트의 합계** 상한이다. 여유를 두는 이유는
    품질이 아니라 잘림 방지다: 잘리면 부분 JSON이 파서로 넘어가고, 지금은 그것이 예외 없이
    폴백돼 조용한 품질 저하가 된다.
    """

    effort: str
    max_tokens: int
    thinking: str = THINKING_ADAPTIVE
    # 재시도 예산. 기본 4회이고, 실패의 대가가 큰 두 단계(본문·콘텐츠 설계)만 6회다 —
    # 그 둘이 실패하면 글이 FAILED로 떨어지거나 골격 없이 나가는데, Anthropic에는 M3 Gemini와
    # 달리 모델 폴백 체인이 없다. 재시도는 재시도 가능한 상태(5xx·429)에서만 쓰이므로 정상
    # 경로의 비용·지연에는 영향이 없다.
    attempts: int = MAX_REQUEST_ATTEMPTS


# 단계별 초기값. **검증이 끝난 최종값이 아니다** — Blog-it 평가 데이터로 비용·지연·품질을
# 비교하기 위한 출발점이고, 실서비스 로그를 기준으로 단계별 effort sweep이 필요하다.
#
# thinking을 11곳 모두 기본(adaptive)으로 둔 이유: 이 호출은 전부 forced tool_choice이고,
# 공식 문서가 "thinking을 끄면 tool_use 블록 대신 도구 호출이 텍스트로 새는 일이 도구 많은
# 작업에서 가장 흔하다"고 경고한다. thinking을 끄는 것 자체는 API에서 합법이지만(실측 200),
# 얻는 것이 없다 — 실측에서 adaptive + effort low의 출력 토큰은 disabled와 사실상 같았다
# (편집 문체 단계: disabled 394 vs adaptive/low 342). 비용은 effort로 조절한다.
STAGE_BUDGETS: dict[str, StageBudget] = {
    # 후보 5개가 서로 다른 관점을 가져야 해서 한 번에 여러 설계를 세운다.
    "m2-topic": StageBudget(effort="high", max_tokens=6000),
    # 고정 루브릭을 순서대로 적용하는 판정이다.
    "m2-title-eval": StageBudget(effort="medium", max_tokens=8000),
    # 키워드 60개를 반복 채점한다 — 비용과 지연의 영향이 가장 큰 단계다.
    "m2-keyword-relevance": StageBudget(effort="low", max_tokens=16000),
    # 입력 범위 안에서 후보를 넓히는 단순 작업이다.
    "m2-keyword-expansion": StageBudget(effort="low", max_tokens=12000),
    # 원고 전체가 따를 제목 방향을 판단한다.
    "m4-title-plan": StageBudget(effort="medium", max_tokens=6000),
    # 준 자료에서 보이는 것과 추정을 분리한다.
    "m4-reference-evidence": StageBudget(effort="medium", max_tokens=8000),
    # 목적과 페르소나를 충돌 없이 결합한다.
    "m4-editorial-style": StageBudget(effort="medium", max_tokens=6000),
    # 섹션 구조와 정보 배치를 정한다. effort는 high → **low**(2026-08-10 사용자 결정:
    # "원고구조설계는 LOW로 하고 본문작성을 HIGH로 해서 속도를 좀 올리자" — 1단계가
    # 156초씩 걸렸다). 설계가 약해지는 하방은 뒤가 받친다: 본문(m4-draft, high)이 실제
    # 문장을 쓰고, 최종 검수(m4-final-review, high)가 자료 대조로 어긋남을 잡는다.
    # 설계 품질 저하가 눈에 띄면 medium부터 되올린다.
    "m4-content-plan": StageBudget(effort="low", max_tokens=16000, attempts=6),
    # 정해진 배치 규칙을 적용한다.
    "m4-seo-plan": StageBudget(effort="medium", max_tokens=6000),
    # 가장 길고 복합적인 최종 산출물이다.
    "m4-draft": StageBudget(effort="high", max_tokens=32000, attempts=6),
    # 본문 맥락과 근거에 맞는 표현 유형을 고른다.
    "m4-card-plan": StageBudget(effort="medium", max_tokens=12000),
    # 완성 원고를 자료와 대조하는 최종 검수(4단계). 새로 쓰지 않고 어긋난 문장만 찾아
    # 고칠 문장을 돌려주므로 출력은 짧다 — 대신 원고 전문과 자료를 모두 읽어야 하고,
    # '자료에 없는데 단정했다'는 판단이 이 단계의 값어치라 effort는 높게 둔다.
    # 회차마다 한 번씩 최대 3회 돌 수 있다(modules/draft/final_review.py).
    "m4-final-review": StageBudget(effort="high", max_tokens=8000),
    # 문장 다듬기(5단계). **이 항목이 빠져 있어 다듬기가 한 번도 돌지 않았다**
    # (2026-08-06 로그: `문장 다듬기 실패 — KeyError: 'm4-polish'`가 매번). 호출부가
    # 실패를 삼키고 원고를 그대로 쓰도록 되어 있어(그 설계는 맞다) 조용히 빠져 있었다.
    #
    # 검수를 마친 원고에서 **표현만** 고칠 자리를 돌려주는 단계다. 새로 쓰지 않으므로
    # 출력이 짧고, 어색한 문장·AI 말투를 알아보는 일이라 판단은 가벼운 편이다.
    "m4-polish": StageBudget(effort="medium", max_tokens=8000),
    # 완성 원고에 대한 비평(마무리 1단계, 2026-08-07). 의견 목록만 내므로 출력은 짧다.
    # high → medium → **low**로 두 번 내렸다(같은 날 사용자 결정: 4단계가 3~4분씩 걸려
    # 멈춘 느낌이다). 2차 비평(GPT)도 low라 두 비평이 같은 눈높이고, 비평의 채택 여부는
    # 통합 단계(high)가 다시 판단하므로 여기서 잃는 것이 통합에서 걸러진다.
    "m4-critique": StageBudget(effort="low", max_tokens=6000),
    # 두 비평을 통합해 원고 전체를 다시 쓴다(마무리 2단계). 출력이 원고 전문이므로
    # 원고 생성과 같은 상한이 필요하다.
    #
    # effort는 high → medium(2026-08-07 사용자 결정, 4단계 체감 속도). 내려도 하방이
    # 코드로 막혀 있다: 재작성은 자리표·길이·콘텐츠 검증을 통과하지 못하면 통째로
    # 버려지고 원본을 쓴다 — 약해지면 '개선 폭이 줄어드는' 쪽으로만 나빠진다. 원고
    # 자체는 m4-draft(high)가 이미 썼다. 재작성 채택률이 눈에 띄게 떨어지면 되올린다.
    "m4-integrate": StageBudget(effort="medium", max_tokens=32000),
    # 스레드 연속 게시물. 근거가 이미 완성된 블로그 본문이라 판단량이 작고, 출력도
    # 스레드 몇 개(합계 1,000자 안쪽)라 짧다(2026-08-06에 되살렸다 — 아래 주석 참고).
    "m4-threads-post": StageBudget(effort="medium", max_tokens=4000),
}

# 자료 검증(M3)의 **모델 하나당** 재시도 횟수.
#
# 처음에는 이 값을 6으로 올렸다(기본 4회의 대기가 1.5+3+6 ≈ 10초뿐이라). 그것은 틀린
# 처방이었다 — 운영에서 gemini-3.5-flash가 여섯 번 연속 500을 내는 동안 사용자가 본 것은
# 더 긴 대기 뒤의 같은 실패였다("계속 재시도만 뜬다"). 한 모델이 혼잡할 때 필요한 것은
# 더 기다리는 것이 아니라 다른 모델로 옮기는 것이다.
#
# 그래서 회복력은 재시도가 아니라 모델 체인(RESEARCH_FALLBACK_MODELS)이 담당하고, 같은
# 모델은 **한 번만** 부른다. 혼잡한 모델은 빨리 실패하지도 않는다 — 실측에서 한 번의 호출이
# 95초를 붙잡고 있다가 500을 냈다(2회 = 190초). 같은 모델을 다시 부르는 것은 그 시간을 한
# 번 더 쓰는 일일 뿐이다.
VERIFY_REQUEST_ATTEMPTS = 1

# 검증 자료 수집 한 호출의 상한. 넘으면 그 모델은 포기하고 다음 모델로 넘어간다.
#
# 공용 클라이언트의 read 타임아웃은 300초다(긴 원고·이미지가 그만큼 걸린다). 그런데 혼잡한
# 모델은 오류를 빨리 주지 않고 붙잡고 있다 — 실측 95초 뒤 500. 그 시간을 기다릴 이유가 없다:
# 정상 응답은 실측 14초였고, 이 단계의 과거 측정도 32~44초였다. 45초면 정상 응답은 통과하고
# 붙잡힌 호출은 잘라 낸다.
RESEARCH_COLLECT_TIMEOUT = 45.0

# 혼잡·응답 없음으로 포기한 모델을 얼마나 건너뛸지(프로세스 로컬).
#
# 이것이 없으면 매 검증이 혼잡한 설정 모델에서 45초를 먼저 버린다. 한 번 확인된 혼잡은
# 잠깐 이어지므로, 그 동안은 다음 모델부터 시작해 사용자가 기다리는 시간을 줄인다.
RESEARCH_MODEL_COOLDOWN = 300.0
_research_model_cooldown: dict[str, float] = {}


def _with_url_context_audit(
    summary: str, requested_urls: list[str], results: list[GeminiUrlContextResult]
) -> str:
    """정리 모델에 URL별 실제 조회 성공 여부를 함께 넘긴다.

    URL 문자열만 보고 내용을 읽었다고 오해하지 않도록, Gemini 도구 결과가 success인 주소와
    그렇지 않은 주소를 명시한다. 원 URL은 이미 같은 provider 요청에 들어간 사용자 자료다.
    """

    if not requested_urls:
        return summary
    status_by_url = {result.requested_url or result.url: result.status for result in results}
    audit = ["URL Context 실제 조회 결과:"]
    for url in requested_urls:
        status = status_by_url.get(url, "not_retrieved")
        audit.append(f"- [{status}] {url}")
    audit.append("success가 아닌 URL의 내용은 확인된 사실로 사용하지 마세요.")
    return f"{summary}\n\n" + "\n".join(audit)


def _model_on_cooldown(model: str) -> bool:
    until = _research_model_cooldown.get(model)
    if until is None:
        return False
    if time.monotonic() >= until:
        _research_model_cooldown.pop(model, None)
        return False
    return True

# 설정된 검증 모델이 혼잡할 때 차례로 시도하는 형제 모델.
#
# 조건은 하나다: 같은 `v1beta/interactions` 엔드포인트에서 `google_search` 도구로
# grounding된 자료를 돌려줄 수 있어야 한다. 아래 목록은 추측이 아니라 실제 프롬프트로
# 호출해 200과 grounding 메타데이터를 확인한 것이다.
#
# **순서를 2026-08-03에 다시 쟀다.** 예전 순서는 2026-07-30의 **한 번씩** 잰 값
# (3.6-flash 9.3초 / 3.5-flash-lite 5.6초 / 2.5-flash 7.2초)을 근거로 최신 모델을
# 앞세웠는데, 한 번의 측정으로는 이 단계에서 중요한 것(편차)을 볼 수 없었다. 같은
# M3 수집 프롬프트로 모델마다 5회씩 재니 결과가 이렇다.
#
#     gemini-3.6-flash       중앙값 34.0초  최소 14.8  최대 69.8   45초 초과 2/5
#     gemini-2.5-flash       중앙값 12.6초  최소 11.1  최대 23.8   45초 초과 0/5
#     gemini-3.5-flash-lite  중앙값 10.0초  최소  8.5  최대 12.3   45초 초과 0/5
#
# 3.6-flash는 고장난 것이 아니라 **느리고 편차가 크다**(전부 HTTP 200이었다). 그런데
# 이 단계에는 45초 상한이 있어서, 세 번에 한 번꼴로 그 45초를 통째로 버리고 다음 모델로
# 넘어간다 — 사용자에게는 "검증이 늘 한참 걸리다가 대체된다"로 보인다.
#
# 그래서 빠르고 **흔들리지 않는** 것을 앞세운다. 품질 때문에 상위 모델을 앞세우던 기존
# 판단은 유지할 근거가 없다: 셋 다 같은 도구로 grounding된 자료를 돌려주고, 실제로 대체
# 모델이 돌려준 자료로 원고까지 정상적으로 나왔다. 3.6-flash는 목록에 남긴다 — 앞의 둘이
# 모두 막혔을 때는 느린 것이 없는 것보다 낫다.
#
# 구글이 모델을 내리면 그 항목은 404가 되고 다음 후보로 넘어간다.
# **2026-08-06: 사용자 결정으로 3.6-flash를 앞세운다.** 위 측정(느리고 편차가 크다)은
# 그대로 유효하다 — 이 단계의 45초 상한에 걸려 대체 모델로 넘어가는 일이 늘 수 있다.
# 그때는 아래 둘이 받아 준다. 품질을 우선한 선택이며, 측정값을 지우지 않고 남겨 둔다.
RESEARCH_FALLBACK_MODELS = (
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
)


async def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
    """지수 백오프 + 지터로 다음 재시도까지 기다린다.

    지터를 넣는 이유: 이미지 여러 장이 병렬로 같은 시각에 실패하면, 지터 없이는 모두
    똑같은 간격으로 다시 몰려들어 혼잡을 되풀이한다. 제공자가 Retry-After 헤더로 명시한
    대기가 있으면 그보다 짧게 기다리지 않는다.
    """
    delay = min(_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _RETRY_MAX_DELAY)
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    await asyncio.sleep(delay + random.uniform(0.0, 0.75))


def _required_api_key(role: RoleConfig) -> str:
    if not role.api_key:
        raise LiveAdapterError(f"{role.api_key_env or 'API key'} is required")
    return role.api_key


def _clamp_score(value: Any, default: float = 50.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(100.0, float(value)))
    return default


# 관계 유형별 소재 점수 상한. 프롬프트에도 적혀 있지만 여기서 한 번 더 강제한다 — 루브릭의
# 보장이 "모델이 지켜 주기를 바라는 것"에 머물면, 모델이 한 번 흔들릴 때마다 무관한 키워드가
# 소재 관련순으로 새어 든다. 상한은 규격이므로 코드가 지킨다.
#
# 소재 관련순은 점수 하한이 아니라 관계 유형으로 NONE·FORCED를 거른다. 이 상한은 모델이
# 관계 유형과 점수를 어긋나게 내더라도 점수가 그 유형의 의미를 벗어나지 않게 한다.
RELATION_SUBJECT_CAP = {
    "DIRECT": 100.0,
    "ADJACENT": 89.0,
    "CONTEXTUAL": 69.0,
    "FORCED": 39.0,
    "NONE": 15.0,
    "AMBIGUOUS": 40.0,
}
# DIRECT는 하한도 있다: 소재 자체라고 판정해 놓고 낮은 점수를 주면 두 판단이 어긋난다.
RELATION_SUBJECT_FLOOR = {"DIRECT": 85.0}
# 소재 점수가 이 미만이면 목적 점수는 아래 상한을 넘지 못한다 — 소재와 무관한 키워드가
# '글 목적에는 맞다'는 이유로 통과하는 길을 막는다(일관성 규칙 1·9).
UNRELATED_SUBJECT_BELOW = 30.0
UNRELATED_PURPOSE_CAP = 40.0


def _capped_by_relation(
    relation: str | None, subject: float | None, purpose: float | None, relevance: float
) -> tuple[float | None, float | None, float]:
    """관계 유형이 정한 상한(과 DIRECT의 하한)을 실제 관련도 점수에 적용한다."""
    if subject is not None and relation in RELATION_SUBJECT_CAP:
        subject = min(subject, RELATION_SUBJECT_CAP[relation])
        subject = max(subject, RELATION_SUBJECT_FLOOR.get(relation, 0.0))
    if subject is not None and subject < UNRELATED_SUBJECT_BELOW and purpose is not None:
        purpose = min(purpose, UNRELATED_PURPOSE_CAP)
    # 종합 점수도 같은 천장을 넘지 않게 한다. 툴팁·추천 배지가 쓰는 값이라, 관계가 NONE인데
    # "소재 연관 90"이 표시되면 루브릭이 막으려던 바로 그 오도가 화면에 남는다.
    if relation in RELATION_SUBJECT_CAP:
        relevance = min(relevance, RELATION_SUBJECT_CAP[relation])
    return subject, purpose, relevance


def _usage_tokens(payload: Any) -> tuple[int | None, int | None]:
    """provider 응답에서 (입력, 출력) 토큰을 읽는다. 형식은 제공자마다 다르다 —
    Anthropic/OpenAI responses는 usage.input_tokens/output_tokens, Gemini interactions는
    usageMetadata.promptTokenCount/candidatesTokenCount. 없으면 (None, None)."""
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if isinstance(input_tokens, int) or isinstance(output_tokens, int):
            return (
                input_tokens if isinstance(input_tokens, int) else None,
                output_tokens if isinstance(output_tokens, int) else None,
            )
    meta = payload.get("usageMetadata")
    if isinstance(meta, dict):
        prompt = meta.get("promptTokenCount")
        candidates = meta.get("candidatesTokenCount")
        return (
            prompt if isinstance(prompt, int) else None,
            candidates if isinstance(candidates, int) else None,
        )
    return None, None


def _provider_from_url(url: str) -> str:
    if "anthropic" in url:
        return "anthropic"
    if "openai" in url:
        return "openai"
    if "googleapis" in url:
        return "gemini"
    return "unknown"


async def _post_json(
    url: str,
    headers: dict[str, str],
    body: Any,
    attempts: int = MAX_REQUEST_ATTEMPTS,
) -> Any:
    """제공자에 JSON을 POST하고 JSON을 돌려준다. 재시도·성능 추적은 _request_with_retries가
    맡는다. 대부분의 provider 호출이 이 경로다.

    `attempts`로 호출부가 재시도 예산을 정할 수 있다 — 단계마다 실패의 대가가 다르다
    (VERIFY_REQUEST_ATTEMPTS 주석 참고)."""
    request_headers = {"content-type": "application/json", **headers}
    model = body.get("model", "") if isinstance(body, dict) else ""
    return await _request_with_retries(
        url,
        str(model),
        lambda: shared_client().post(url, headers=request_headers, json=body),
        attempts=attempts,
    )


def anthropic_request_body(
    *,
    model: str,
    stage: str,
    system: str,
    content: Any,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    output_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Anthropic Messages 요청 본문을 한 경로에서 조립한다(3-3).

    `temperature`·`top_p`·`top_k`는 여기서 **애초에 만들지 않는다** — 제거가 아니라 미생성이다.
    HTTP 400을 받은 뒤 파라미터를 빼고 재호출하는 우회 경로도 두지 않는다: 그런 경로는 첫
    요청이 늘 실패하는 것을 정상으로 만든다.

    thinking이 adaptive면 필드를 아예 넣지 않는다. Opus 5는 thinking이 기본 ON이고
    `{"type":"adaptive"}`가 기본값과 동일하므로, 생략이 곧 adaptive다. 그러면 테스트가
    `"thinking" not in body`로 의도를 그대로 단정할 수 있다.
    """
    budget = STAGE_BUDGETS[stage]
    if budget.effort not in EFFORT_LEVELS:
        raise ValueError(
            f"{stage}: effort는 low·medium·high·xhigh·max 중 하나여야 합니다: {budget.effort}"
        )
    if budget.thinking not in {THINKING_ADAPTIVE, THINKING_DISABLED}:
        raise ValueError(
            f"{stage}: thinking은 adaptive 또는 disabled여야 합니다: {budget.thinking}"
        )
    if (
        budget.thinking == THINKING_DISABLED
        and budget.effort not in EFFORT_ALLOWED_WITH_THINKING_DISABLED
    ):
        raise ValueError(
            f"{stage}: thinking을 끈 요청은 effort를 high 이하로 두어야 합니다"
            f"(Anthropic이 400으로 거절합니다): effort={budget.effort}"
        )
    if budget.max_tokens <= 0:
        raise ValueError(f"{stage}: max_tokens는 1 이상이어야 합니다: {budget.max_tokens}")

    output_config: dict[str, Any] = {"effort": budget.effort}
    if output_format is not None:
        output_config["format"] = output_format

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": budget.max_tokens,
        "output_config": output_config,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": tool_schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    if budget.thinking == THINKING_DISABLED:
        body["thinking"] = {"type": "disabled"}
    return body


def check_anthropic_stop_reason(
    payload: Any, *, stage: str, model: str, max_tokens: int
) -> None:
    """HTTP 200이어도 쓸 수 없는 응답을 걸러 낸다.

    Opus 5는 거절·컨텍스트 초과·출력 잘림을 오류 응답이 아니라 200 + `stop_reason`으로 알려
    준다. 예전에는 이 값을 읽는 코드가 한 곳도 없어서 잘린 `tool_use.input`이 부분 dict로
    파서에 넘어갔고, `final_post_from_json`이 예외 없이 폴백해 **조용한 품질 저하**로 나갔다.
    """
    if not isinstance(payload, dict):
        return
    stop_reason = payload.get("stop_reason")
    if stop_reason in (None, "tool_use", "end_turn", "stop_sequence", "pause_turn"):
        return
    if stop_reason == "refusal":
        details = payload.get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        logger.warning(
            "provider 거절 | 단계=%s 모델=%s 분류=%s", stage, model, category or "unknown"
        )
        raise ProviderRefusedError(stage=stage, model=model, category=category)
    if stop_reason == "model_context_window_exceeded":
        logger.warning("provider 컨텍스트 초과 | 단계=%s 모델=%s", stage, model)
        raise ProviderContextExceededError(stage=stage, model=model)
    if stop_reason == "max_tokens":
        logger.warning(
            "provider 출력 잘림 | 단계=%s 모델=%s max_tokens=%s", stage, model, max_tokens
        )
        raise ProviderTruncatedError(stage=stage, model=model, max_tokens=max_tokens)
    logger.warning(
        "알 수 없는 stop_reason | 단계=%s 모델=%s stop_reason=%s", stage, model, stop_reason
    )


async def anthropic_tool_call(
    *,
    api_key: str,
    model: str,
    stage: str,
    system: str,
    content: Any,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
) -> Any:
    """Anthropic 도구 호출 한 번. 본문 조립 → 전송 → stop_reason 검사까지 한 문으로 지난다.

    `_post_json`/`_request_with_retries`/`http.py`는 OpenAI·Gemini와 공용이라 손대지 않는다 —
    Anthropic 전용 규칙을 그 안에 넣으면 세 공급자 경로가 함께 흔들린다. 이 함수는 그 위에
    얹은 얇은 층이다.
    """
    budget = STAGE_BUDGETS[stage]
    body = anthropic_request_body(
        model=model,
        stage=stage,
        system=system,
        content=content,
        tool_name=tool_name,
        tool_description=tool_description,
        tool_schema=tool_schema,
    )
    payload = await _post_json(
        ANTHROPIC_MESSAGES_URL,
        {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        body,
        attempts=budget.attempts,
    )
    check_anthropic_stop_reason(
        payload, stage=stage, model=model, max_tokens=budget.max_tokens
    )
    return payload


async def _post_multipart(
    url: str,
    headers: dict[str, str],
    data: dict[str, Any],
    files: dict[str, Any] | list[tuple[str, Any]],
    model: str = "",
) -> Any:
    """제공자에 multipart/form-data를 POST한다(이미지 편집 등 파일 업로드 경로). content-type은
    httpx가 boundary와 함께 자동으로 붙이므로 직접 지정하지 않는다."""
    return await _request_with_retries(
        url,
        model,
        lambda: shared_client().post(url, headers=headers, data=data, files=files),
    )


def _transport_detail(error: Exception) -> str:
    """연결 계층 예외를 로그에 쓸 한 줄로.

    httpx의 연결 예외는 **메시지가 비어 있는 경우가 흔하다** — 회선이 끊기면
    `ReadError()`·`ConnectError()`처럼 인자 없이 올라온다. 그대로 찍으면 로그가
    "재시도: "에서 끝나 무엇 때문에 끊겼는지 알 수 없다(사용자 보고 2026-08-11).
    그래서 종류 이름을 언제나 남기고, 메시지가 있을 때만 뒤에 붙인다.
    """
    detail = str(error).strip()
    name = type(error).__name__
    return f"{name}: {detail}" if detail else name


async def _request_with_retries(
    url: str, model: str, send, attempts: int = MAX_REQUEST_ATTEMPTS
) -> Any:
    """POST 재시도·성능 추적 공통 코어. `send`는 매 시도마다 새 요청 코루틴을 돌려주는
    호출가능 객체다(JSON이든 multipart든 호출부가 만든다).

    일시적 오류(5xx·429·네트워크)는 지수 백오프로 재시도하고, 결정적 오류(4xx 대부분)는
    곧바로 실패로 올린다. 클라이언트는 공유 keep-alive 풀(http.shared_client)을 쓴다 —
    호출마다 새로 만들면 TCP·TLS 핸드셰이크가 반복되고, 한 번의 생성이 provider를 십수 번
    부른다. 호출 1건의 소요·시도 횟수·응답 크기·토큰은 성능 추적(perf)에 남긴다."""
    call_started = time.monotonic()

    def _trace(status: int | str, attempts: int, size: int, payload: Any, ok: bool) -> None:
        input_tokens, output_tokens = _usage_tokens(payload)
        perf.record_provider_call(
            provider=_provider_from_url(url),
            model=str(model),
            start=call_started,
            end=time.monotonic(),
            status=status,
            attempts=attempts,
            response_bytes=size,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ok=ok,
        )

    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        try:
            response = await send()
        except (httpx.TransportError, httpx.TimeoutException) as error:
            # 연결 끊김·타임아웃도 대개 일시적이다. 마지막 시도였으면 실패로 올린다.
            if attempt >= attempts:
                _trace("transport-error", attempt, 0, None, ok=False)
                raise LiveAdapterError(
                    f"provider request failed after {attempt} attempts: "
                    f"{_transport_detail(error)}"
                ) from error
            # **어느 모델이 얼마나 붙잡고 있었는지 함께 남긴다**(2026-08-13). 종류와
            # provider만 있으면 "ReadTimeout | openai"가 되는데, 그 한 줄로는 이미지
            # 생성이 멈춘 것인지 비평이 멈춘 것인지 알 수 없고 몇 초를 태웠는지도 모른다.
            # 타임아웃 값을 조정하려면 그 두 가지가 있어야 한다.
            logger.warning(
                "provider 연결 오류 (%d/%d) - 재시도: %s | %s %s (%.1f초 대기)",
                attempt,
                attempts,
                _transport_detail(error),
                _provider_from_url(url),
                model or "-",
                time.monotonic() - attempt_started,
            )
            await _sleep_before_retry(attempt, None)
            continue

        response_size = len(response.content)
        if response.is_error:
            # 상태를 먼저 본다. 502/503 프록시가 HTML을 돌려줄 때 response.json()부터
            # 호출하면 JSONDecodeError로 빠져 아래 5xx 재시도를 전혀 타지 못했다.
            try:
                payload = response.json() if response_size else None
            except ValueError:
                payload = None
            # 오류일 때만 전체 응답을 문자열로 디코딩한다. 성공한 이미지 응답은 수 MB의
            # base64 JSON이라 text로 한 번, json으로 또 한 번 풀면 메모리·CPU가 이중으로 든다.
            text = response.text
            message = text
            if isinstance(payload, dict) and "error" in payload:
                import json

                message = json.dumps(payload["error"])
            # 일시적 과부하·레이트리밋이면 잠깐 쉬고 다시 부른다. 마지막 시도까지 실패했을
            # 때만 오류로 올린다 — 그래야 검증·원고·이미지가 스파이크 한 번에 무너지지 않는다.
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                logger.warning(
                    "provider %d (%d/%d) - 재시도: %s",
                    response.status_code,
                    attempt,
                    attempts,
                    message[:200],
                )
                await _sleep_before_retry(attempt, response.headers.get("retry-after"))
                continue
            _trace(response.status_code, attempt, response_size, payload, ok=False)
            if response.status_code in _RETRYABLE_STATUS:
                # 혼잡을 다 기다려 본 실패. 설정 오류와 구분해서 올린다 — 화면이 안내할
                # 말이 다르다(ProviderOverloadedError 주석 참고).
                raise ProviderOverloadedError(
                    provider=_provider_from_url(url),
                    model=str(model),
                    status=response.status_code,
                    detail=message,
                )
            raise ProviderRequestError(
                response.status_code,
                message,
                payload,
            )
        try:
            payload = response.json() if response_size else None
        except ValueError as error:
            _trace(response.status_code, attempt, response_size, None, ok=False)
            raise LiveAdapterError(
                f"provider returned invalid JSON with {response.status_code}"
            ) from error
        _trace(response.status_code, attempt, response_size, payload, ok=True)
        return payload

    # 루프는 반드시 return이나 raise로 끝난다. 여기 도달하면 로직 오류이므로 방어적으로 올린다.
    raise LiveAdapterError("provider request failed: 재시도 한도를 모두 소진했습니다")


# 검증 화면에 보여 주는 자료 수의 상한. 개수는 소재마다 다르다 — 관련 있는 만큼만 나온다.
# 사용자 입력은 참고자료를 최대 10개 받는다. 전부 URL이고 모두 직접 조회에 성공해도 하나도
# M4에서 사라지지 않도록 후보에 붙이는 최종 상한도 10으로 맞춘다. 정리 모델 출력 자체는
# 비용을 위해 최대 5개지만, 아래 helper가 검증된 URL 원본을 앞에 보강한다.
INTENT_SOURCE_MAX = 10


def _reference_urls(analysis_input: WebSearchAnalysisInput) -> list[str]:
    """사용자가 직접 입력한 URL을 중복 없이 입력 순서대로 돌려준다.

    네이버 블로그의 데스크톱 주소는 모바일판으로 바꿔 넘긴다(2026-08-10) — 데스크톱
    페이지는 본문이 iframe 안에 있는 껍데기라, URL Context가 그대로 읽으면 본문 없는
    틀만 '확인'된다. 모바일판은 본문이 HTML에 그대로 있다. 네이버가 아닌 주소는
    손대지 않는다.
    """

    return list(
        dict.fromkeys(
            to_mobile_url(material.value.strip()) or material.value.strip()
            for material in analysis_input.input.reference_materials
            if material.type.value == "URL"
            and material.value.strip()
            and is_public_reference_url(material.value.strip())
        )
    )


def _sources_for_candidate(
    chosen: list[SearchSource],
    collected: list[SearchSource],
    pinned_urls: list[str] | None = None,
) -> list[SearchSource]:
    """후보에 붙일 자료. 상한은 INTENT_SOURCE_MAX이고, **모자라면 수집분으로 채운다.**

    예전에는 정리 모델이 고른 것만 그대로 썼다("관련 있는 만큼만"). 그러면 실제로 자료를
    여덟 개 찾아 놓고도 화면에는 셋만 떴다 — 나머지는 어디에도 남지 않고 사라졌다
    (2026-08-07 사용자 신고: "자료가 지금 너무 적게 수집돼").

    채우는 자료는 **이미 검색으로 찾아 둔 실제 출처**다. 새 호출도, 지어낸 URL도 없다.
    다만 모델이 고른 것이 아니므로 source_type·relevance_score가 비어 있는데, 그것이
    "모델이 고르지 않았다"는 사실을 그대로 말해 준다 — 화면은 그 자료를 뒤에 세우고
    분류 배지 없이 보여 준다.

    사용자가 직접 준 URL은 URL Context로 성공 조회된 경우 맨 앞에 고정한다. 정리 모델이
    다른 출처 다섯 개를 골라도 직접 확인한 참고 페이지가 M4 원고 입력에서 사라지면 안 된다.
    나머지는 모델이 고른 것, 수집분 순이다.
    """
    pinned_order = list(dict.fromkeys(pinned_urls or []))
    chosen_by_url = {source.url: source for source in chosen}
    collected_by_url = {source.url: source for source in collected}
    # 정리 모델이 직접 URL을 골랐다면 그 모델이 붙인 안전한 한 줄 요약·관련도 판정을
    # 보존하고, 고르지 않았을 때만 수집 원본을 사용한다.
    pinned = [
        chosen_by_url.get(url) or collected_by_url[url]
        for url in pinned_order
        if url in chosen_by_url or url in collected_by_url
    ]
    filled = dedupe_sources([*pinned, *chosen, *collected])
    return filled[:INTENT_SOURCE_MAX]


# 같은 입력의 '다시 검증'이 수집을 되풀이하지 않게 하는 짧은 캐시. 수집(grounded 검색)이
# 검증에서 가장 긴 구간이라, 몇 분 안의 재검증은 같은 웹을 다시 훑어 같은 자료를 받는다.
# TTL을 짧게 두는 이유: 검증의 존재 이유가 '지금'의 자료라서다 — 오래된 수집분을 신선한
# 것처럼 내주면 안 된다. 요약(의도 후보 생성)은 캐시하지 않아 재검증마다 새로 만든다.
_RESEARCH_CACHE_TTL_SECONDS = 600.0
_RESEARCH_CACHE_MAX_ENTRIES = 50


async def _say(on_note: "OnResearchNote | None", message: str) -> None:
    """진행 한 줄을 호출부로 흘린다. 보고가 수집을 죽이지 않는다 — 실패는 삼킨다."""
    if on_note is None:
        return
    try:
        await on_note(message)
    except Exception as error:  # noqa: BLE001
        logger.debug("수집 진행 보고 실패(무시): %s", error)


async def _note(on_note: "OnResearchNote | None", added: int, label: str) -> None:
    """보강으로 늘어난 자료를 한 줄로. 하나도 못 늘렸으면 그렇다고 적는다 —
    조용히 넘어가면 사용자는 그 단계가 돌았는지조차 알 수 없다."""
    if added > 0:
        await _say(on_note, f"{label} {added}건을 자료에 추가했습니다.")
    else:
        # 조사를 붙이면 받침에 따라 틀린다("기사은"). 줄표로 잇는다.
        await _say(on_note, f"{label} — 찾지 못했습니다.")


class GeminiResearchAnalyzer:
    """M3. 2단계 모두 Gemini다 — 자료를 모으고, 그걸 방향 후보로 바꾼다.

    2026-08-07 이전에는 정리를 OpenAI가 맡았다(`GeminiOpenAiResearchAnalyzer`). 사용자
    결정으로 한 provider로 모았다. **수집 호출은 그대로다** — 자료의 질을 정하는 것은
    grounding 검색이지 정리 모델이 아니라서, 이 교체로 수집이 달라지지 않는다.

    두 호출을 합치지 않은 이유:

    - 수집만 캐시할 수 있다(TTL 600초). '다시 검증'이 웹을 다시 훑지 않는다.
    - 정리가 실패해도 수집한 자료로 후보 하나를 세우는 폴백이 산다(_sources_only_result).
    - grounding과 responseSchema를 한 호출에 묶었을 때의 동작을 확인하지 못했다.
    """

    def __init__(
        self,
        gemini_role: RoleConfig,
        summary_role: RoleConfig,
        blog_research=None,
        news_research=None,
    ):
        self._gemini_role = gemini_role
        self._summary_role = summary_role
        self._gemini_key = _required_api_key(gemini_role)
        self._summary_key = _required_api_key(summary_role)
        # 네이버 블로그 보강 수집기(llm/naver_blog.NaverBlogResearch, 없으면 None).
        # 구글 검색이 잘 못 잡는 네이버 생태계의 실사용 후기를 자료에 더한다
        # (2026-08-10 사용자 결정). 없으면 예전 그대로 구글 수집만 돈다.
        self._blog_research = blog_research
        # 네이버 뉴스 보강 수집기(llm/naver_news.NaverNewsResearch, 없으면 None).
        # 블로그가 '사람들이 써 보니 어땠나'라면 이쪽은 '지금 무슨 일이 있었나'다
        # (2026-08-11 사용자 지시 — 검증 자료에 관련된 최신 기사가 들어와야 한다).
        self._news_research = news_research
        # key -> (만료 시각(monotonic), 브리핑, 출처). 프로세스 로컬 — 재검증은 대개
        # 같은 세션·같은 프로세스에서 눌린다.
        self._research_cache: dict[
            str, tuple[float, str, list[SearchSource], list[str]]
        ] = {}

    def _research_cache_key(self, analysis_input: WebSearchAnalysisInput) -> str:
        blog_input = analysis_input.input
        material = "|".join(
            f"{m.type.value}:{hashlib.sha256(m.value.encode()).hexdigest()[:16]}"
            for m in blog_input.reference_materials
        )
        raw = "|".join(
            [
                blog_input.topic,
                blog_input.subject or "",
                ",".join(blog_input.purpose or []),
                ",".join(blog_input.keywords),
                # 같은 소재라도 고른 검색 키워드가 다르면 다른 수집이다 — 키에서 빠지면
                # 키워드를 바꿔 재검증해도 이전 수집분을 그대로 돌려준다.
                ",".join(analysis_input.selected_keywords),
                material,
                self._gemini_role.model,
                analysis_input.prompt_version,
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _collect_research(
        self, analysis_input: WebSearchAnalysisInput
    ) -> tuple[str, list[SearchSource], bool, list[str]]:
        """설정된 모델로 자료를 모으고, 그 모델이 혼잡하면 형제 모델로 넘긴다.

        재시도만으로는 풀리지 않는 실패가 있다. 운영에서 `gemini-3.5-flash`가
        `500 "currently experiencing high demand"`를 여섯 번 연속 냈고, 재시도 예산을
        늘려도 사용자가 보는 것은 더 긴 대기 뒤의 같은 실패였다. 한 모델을 오래 기다리는
        것보다 다른 모델로 옮기는 것이 빠르고 확실하다.

        실측(2026-07-30, 같은 엔드포인트·같은 도구·같은 프롬프트):

            gemini-3.5-flash       200  46,826ms   ← 설정값. 성공해도 47초
            gemini-3.6-flash       200   9,329ms
            gemini-3.5-flash-lite  200   5,550ms
            gemini-2.5-flash       200   7,176ms

        전부 google_search grounding이 붙어 돌아왔다. 대안이 5~8배 빠른 순간도 있으므로,
        폴백은 품질을 깎는 선택이 아니라 같은 일을 해내는 다른 길이다.
        """
        chain = [self._gemini_role.model]
        chain += [m for m in RESEARCH_FALLBACK_MODELS if m != self._gemini_role.model]
        # 방금 혼잡했던 모델은 잠시 건너뛴다. 전부 쿨다운이면 그래도 순서대로 시도한다 —
        # 건너뛰기가 "아무것도 시도하지 않음"이 되어서는 안 된다.
        models = [model for model in chain if not _model_on_cooldown(model)] or chain

        for index, model in enumerate(models):
            try:
                payload = await asyncio.wait_for(
                    self._collect_with(model, analysis_input),
                    timeout=RESEARCH_COLLECT_TIMEOUT,
                )
                # **본문 추출까지 여기서 한다**(2026-08-12 사용자 신고). 예전에는 루프 밖에서
                # 풀었는데, 200으로 왔지만 최종 본문이 없는 응답이 그 자리에서 예외가 되어
                # 폴백 모델을 하나도 시도하지 못한 채 검증이 통째로 실패했다 — 화면에는
                # 방향 후보 4개 대신 '자료를 모으는 중 오류가 났습니다' 한 장만 남았다.
                text = extract_gemini_interaction_text(payload)
            except ProviderEmptyResponseError:
                # 검색은 다 돌았는데 모델이 답을 쓰지 않았다. 설정 문제가 아니므로 같은 일을
                # 다음 모델로 다시 시킨다 — 폴백 목록이 있는 이유가 이것이다.
                if index == len(models) - 1:
                    raise
                logger.warning(
                    "검증 자료 수집: %s가 본문 없이 응답했습니다 - %s로 대체합니다",
                    model,
                    models[index + 1],
                )
                continue
            except (ProviderOverloadedError, asyncio.TimeoutError) as error:
                _research_model_cooldown[model] = (
                    time.monotonic() + RESEARCH_MODEL_COOLDOWN
                )
                if index == len(models) - 1:
                    if isinstance(error, asyncio.TimeoutError):
                        raise ProviderOverloadedError(
                            provider="gemini",
                            model=model,
                            status=504,
                            detail=f"{RESEARCH_COLLECT_TIMEOUT:.0f}초 안에 응답하지 않았습니다",
                        ) from error
                    raise
                logger.warning(
                    "검증 자료 수집: %s %s - %s로 대체합니다",
                    model,
                    "혼잡(%d)" % error.status
                    if isinstance(error, ProviderOverloadedError)
                    else f"{RESEARCH_COLLECT_TIMEOUT:.0f}초 무응답",
                    models[index + 1],
                )
                continue
            except LiveAdapterError as error:
                # 설정된 모델의 결정적 오류(잘못된 키·요청)는 그대로 올린다 — 가려서는
                # 안 되는 문제다. 폴백 목록의 항목은 구글이 모델을 내리면 404가 되므로,
                # 그때는 다음 후보로 넘어간다(오래된 목록 하나가 검증을 죽이지 않게).
                if index == 0 or index == len(models) - 1:
                    raise
                logger.warning("검증 자료 수집: 폴백 모델 %s 사용 불가(%s)", model, error)
                continue
            if index:
                logger.info("검증 자료 수집: %s로 성공했습니다(설정값 대체)", model)
            url_results = extract_gemini_url_context_results(payload)
            requested_urls = _reference_urls(analysis_input)
            cacheable = True
            successful_urls: list[str] = []
            if requested_urls:
                requested_results = [
                    result
                    for result in url_results
                    if (result.requested_url or result.url) in requested_urls
                ]
                succeeded = sum(result.status == "success" for result in requested_results)
                # URL을 못 읽은 결과를 10분 캐시에 넣으면 '다시 검증'도 직접 조회를 하지
                # 않는다. 모든 입력 URL의 success가 확인된 경우에만 캐시한다.
                successful_url_set = {
                    result.requested_url or result.url
                    for result in requested_results
                    if result.status == "success"
                }
                successful_urls = [url for url in requested_urls if url in successful_url_set]
                cacheable = len(successful_urls) == len(requested_urls)
                logger.info(
                    "검증 URL Context 완료 | 요청 %d건 | 성공 %d건 | 미확인·실패 %d건",
                    len(requested_urls),
                    succeeded,
                    len(requested_urls) - succeeded,
                )
            return (
                _with_url_context_audit(text, requested_urls, url_results),
                # 프롬프트에 쓰지 말라고 적어도 grounding이 무엇을 물어 올지는 우리가
                # 정하지 못한다. 마지막에 한 번 더 코드로 막는다(2026-08-11 사용자 지시:
                # "나무위키나 디씨인사이드 같은 것은 안 돼").
                drop_blocked_sources(
                    extract_gemini_interaction_sources(payload), where="수집"
                ),
                cacheable,
                successful_urls,
            )
        raise LiveAdapterError("검증 자료 수집: 사용할 수 있는 모델이 없습니다")

    async def _collect_with(self, model: str, analysis_input: WebSearchAnalysisInput):
        reference_urls = _reference_urls(analysis_input)
        # URL이 없을 때까지 url_context를 켜면 모델이 검색으로 찾은 임의 페이지까지 깊게 읽어
        # 비용·시간이 늘 수 있다. 사용자가 명시한 URL이 있을 때만 공식 권장 조합을 쓴다.
        tools = [{"type": "google_search"}]
        if reference_urls:
            tools.insert(0, {"type": "url_context"})
        return await _post_json(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            {"x-goog-api-key": self._gemini_key},
            {
                "model": model,
                "input": prompts.research_collect_prompt(analysis_input),
                "system_instruction": prompts.RESEARCH_SYSTEM_PROMPT,
                "tools": tools,
                # 후속 interaction을 잇지 않는 단발성 수집이다. 기본 store=true로 사용자
                # URL과 자료를 별도 보관할 이유가 없으므로 stateless로 보낸다.
                "store": False,
                # 그냥 두면 이 호출은 Google 검색 6번에 걸쳐 사고 토큰 4,000개를 써서 출력
                # 400개를 냈다 — verify 팝업이 걸린 1분의 대부분이다. 블로그 브리핑용 사실
                # 수집에 그만한 숙고는 필요 없다.
                #
                # 세 주제로 측정: default 44s 중앙값 / 6.3 출처, high 55s / 6.0, medium 42s
                # / 4.7, low 32s / 6.3. `low`가 가장 빠르면서 출처도 뒤지지 않는다.
                #
                # 예전에는 medium을 골라 약 10s를 자료 여유와 맞바꿨다 — 하지만 운영에서는
                # "한 번만 검색"을 무시하고 134s를 썼다(verify 팝업 첫 단계 전체). 그 비용이면
                # 절충이 성립하지 않는다: `low`가 모델이 제자리를 맴돌지 않게 하는 설정이다.
                #
                # snake_case에 유의: 이 엔드포인트는 `generationConfig`를 거부한다.
                "generation_config": {"thinking_level": "low"},
            },
            attempts=VERIFY_REQUEST_ATTEMPTS,
        )

    async def _collect_naver_blog(self, analysis_input: WebSearchAnalysisInput):
        """네이버 블로그 보강 수집. 질의는 좁은 것부터: 고른 트렌드 키워드 → 소재(제목) →
        입력 키워드. 실패는 호출부가 삼킨다 — 보강이 검증을 죽이지 않는다."""
        with perf.span("naver_blog_research") as meta:
            seen: set[str] = set()
            queries: list[str] = []
            for candidate in [
                *analysis_input.selected_keywords,
                analysis_input.input.topic,
                *analysis_input.input.keywords,
            ]:
                cleaned = (candidate or "").strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    queries.append(cleaned)
            posts = await self._blog_research.collect(queries[:3])
            meta["posts"] = len(posts)
            return posts

    async def _collect_naver_news(self, analysis_input: WebSearchAnalysisInput):
        """네이버 뉴스 보강 수집(최신순). 질의는 좁은 것부터: 고른 트렌드 키워드 → 소재
        → 입력 키워드. 실패는 호출부가 삼킨다 — 보강이 검증을 죽이지 않는다.

        블로그와 달리 질의를 끝까지 돈다(naver_news.collect 참고) — 뉴스는 질의마다 다른
        사건이 잡히고, 최근 기사가 한 질의에만 있는 일이 흔하다.
        """
        with perf.span("naver_news_research") as meta:
            seen: set[str] = set()
            queries: list[str] = []
            for candidate in [
                *analysis_input.selected_keywords,
                analysis_input.input.topic,
                *analysis_input.input.keywords,
            ]:
                cleaned = (candidate or "").strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    queries.append(cleaned)
            articles = await self._news_research.collect(queries[:3])
            meta["articles"] = len(articles)
            return articles

    def _merged_naver_news(
        self, summary: str, sources: list[SearchSource], results: list
    ) -> tuple[str, list[SearchSource]]:
        """최신 기사를 브리핑·출처 목록에 합친다. 실패(예외)면 원본 그대로.

        **발행일을 항상 함께 싣는다.** 이 자료의 값어치는 최신성이라, 언제 것인지 없이는
        요약 모델도 사용자도 그것을 판단할 수 없다. 본문을 못 읽은 기사(연예·스포츠는
        본문이 JS로 그려진다)는 검색 요약이 그 자리를 대신하고, 그 사실을 숨기지 않는다.
        """
        result = results[0] if results else []
        if isinstance(result, BaseException):
            logger.warning("네이버 뉴스 보강 실패(무시하고 진행) | %s", result)
            return summary, sources
        existing_urls = {source.url for source in sources}
        articles = [article for article in result if article.url not in existing_urls]
        if not articles:
            return summary, sources
        merged = [
            *sources,
            *(
                SearchSource(title=article.title, url=article.url, snippet=article.snippet)
                for article in articles
            ),
        ]
        lines = [
            "",
            "[관련 최신 기사 — 네이버 뉴스 검색 API로 최신순 수집]",
            "아래는 이 소재와 관련해 최근 보도된 기사다. 날짜를 반드시 확인하고, 사실 확인"
            " 근거로만 쓰며 문장을 그대로 옮기지 않는다. 기사에 없는 수치·발표를 만들지 않는다.",
        ]
        for article in articles:
            dated = f" (보도 {article.published_at})" if article.published_at else " (보도일 미상)"
            lines.append(f"■ {article.title}{dated} — {article.url}")
            lines.append(article.excerpt or article.snippet)
        logger.info("네이버 뉴스 보강 | 최신 기사 %d건을 자료에 추가", len(articles))
        return summary + "\n" + "\n".join(lines), merged

    def _merged_naver_blog(
        self, summary: str, sources: list[SearchSource], results: list
    ) -> tuple[str, list[SearchSource]]:
        """블로그 글을 브리핑·출처 목록에 합친다. 실패(예외)면 원본 그대로.

        출처 목록에 들어가므로 요약 모델이 sourceIndex로 인용할 수 있고, 검증 팝업에도
        일반 자료와 똑같이 나타나 사용자가 넣고 뺄 수 있다. 본문 발췌는 브리핑 뒤에
        붙는다 — 요약 모델이 '실사용 후기'라는 사실 근거를 실제로 읽게 하기 위해서다.
        """
        result = results[0] if results else []
        if isinstance(result, BaseException):
            logger.warning("네이버 블로그 보강 실패(무시하고 진행) | %s", result)
            return summary, sources
        existing_urls = {source.url for source in sources}
        posts = [post for post in result if post.url not in existing_urls]
        if not posts:
            return summary, sources
        merged = [
            *sources,
            *(
                SearchSource(title=post.title, url=post.url, snippet=post.snippet)
                for post in posts
            ),
        ]
        lines = [
            "",
            "[네이버 블로그 실사용 글 — 네이버 검색 API로 수집한 보강 자료]",
            "아래는 실제 사용자들의 후기·경험 글이다. 사실 확인 근거로만 쓰고 문장을 그대로 옮기지 않는다.",
        ]
        for post in posts:
            dated = f" (작성 {post.posted_at})" if post.posted_at else ""
            lines.append(f"■ {post.title}{dated} — {post.url}")
            lines.append(post.excerpt)
        logger.info("네이버 블로그 보강 | 실사용 글 %d건을 자료에 추가", len(posts))
        return summary + "\n" + "\n".join(lines), merged

    async def _summarize_intent(
        self,
        analysis_input: WebSearchAnalysisInput,
        summary: str,
        sources: list[SearchSource],
        successful_reference_urls: list[str],
        *,
        sources_pending: bool = False,
    ) -> IntentValidationResult:
        payload = await _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._summary_role.model}:generateContent",
            {"x-goog-api-key": self._summary_key},
            {
                "systemInstruction": {"parts": [{"text": prompts.INTENT_SYSTEM_PROMPT}]},
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": prompts.research_summarize_prompt(
                                    analysis_input,
                                    summary,
                                    sources,
                                    successful_reference_urls=successful_reference_urls,
                                    sources_pending=sources_pending,
                                )
                            }
                        ],
                    }
                ],
                # camelCase에 유의: 수집이 쓰는 `interactions`와 달리 이 엔드포인트는
                # `generationConfig`를 받는다(2026-08-07 실제 호출로 확인).
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": GEMINI_INTENT_SCHEMA,
                    # 정리는 이미 모아 둔 자료를 가르는 일이라 길게 숙고할 것이 없다.
                    # 수집 단계와 같은 이유로 낮게 둔다.
                    "thinkingConfig": {"thinkingLevel": "low"},
                },
            },
        )

        parsed = extract_json_object(extract_gemini_text(payload))
        raw_candidates = parsed.get("intentCandidates")
        raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []

        candidates: list[IntentCandidate] = []
        for index, item in enumerate(raw_candidates[: prompts.INTENT_CANDIDATE_COUNT]):
            source = item if isinstance(item, dict) else {}
            keywords = string_array(source.get("keywords")) or analysis_input.input.keywords
            candidates.append(
                IntentCandidate(
                    intent_id=f"{analysis_input.post_id}_intent_{index + 1}",
                    title=string_value(
                        source.get("title"),
                        f"{analysis_input.input.topic} 독자 의도 {index + 1}",
                    ),
                    target_reader=string_value(source.get("targetReader"), "블로그 독자"),
                    # 사용자에게 보이는 설명이라 내부 처리 얘기(Snippet 비었다·sources 제외 등)를
                    # 걷어낸다 — 프롬프트로도 막지만 모델이 어겼을 때의 마지막 안전망.
                    rationale=strip_internal_notes(string_value(source.get("rationale"))),
                    keywords=keywords,
                    # 개수는 모델이 관련도에 따라 정하고(소재마다 다르다) 최대 5개.
                    # 다만 하나도 고르지 않았을 때는 검색이 실제로 찾아 둔 grounded 자료로
                    # 대신한다 — 자료를 지어내는 게 아니라 이미 찾은 것이다.
                    # 새 형식은 sourceIndex(수집 목록 참조)다. 구형 응답(제목·URL 직접
                    # 서술)이 오면 예전 파서로 받되, 수집 목록에 실재하는 URL만 통과시킨다
                    # (allowed_sources) — 어느 형식이든 지어낸 출처는 살아남지 못한다.
                    sources=_sources_for_candidate(
                        sources_from_indexes(source.get("sources"), sources)
                        or sources_value(source.get("sources"), allowed_sources=sources),
                        sources,
                        successful_reference_urls,
                    ),
                )
            )

        if not candidates:
            raise LiveAdapterError("정리 모델이 의도 후보를 돌려주지 않았습니다")

        return IntentValidationResult(
            prompt_version=analysis_input.prompt_version,
            provider="gemini",
            model=f"{self._summary_role.model} / {self._gemini_role.model}",
            analyzed_at=_now(),
            intent_candidates=candidates,
            # 화면이 '외 N개'를 적을 수 있게 **찾아 온 총 개수**를 남긴다. 방향 하나에
            # 붙는 자료는 INTENT_SOURCE_MAX로 잘리므로 이 숫자가 없으면 사용자는 검색이
            # 그만큼밖에 못 찾은 줄로 읽는다.
            collected_source_count=len(sources),
        )

    def _sources_only_result(
        self,
        analysis_input: WebSearchAnalysisInput,
        sources: list[SearchSource],
        successful_reference_urls: list[str],
    ) -> IntentValidationResult:
        """요약 단계가 실패했을 때, 검색이 이미 찾아 둔 자료로 후보 하나를 세운다.

        검색은 성공했는데 요약 모델이 실패했다고 전부 버리면 사용자는 자료가 실제로 있는데도
        "검색 결과가 없습니다" 앞에서 막힌다. 자료를 지어내지 않고, 찾은 것만 그대로 넘긴다.
        """
        return IntentValidationResult(
            prompt_version=analysis_input.prompt_version,
            provider="gemini",
            model=self._gemini_role.model,
            analyzed_at=_now(),
            intent_candidates=[
                IntentCandidate(
                    intent_id=f"{analysis_input.post_id}_intent_1",
                    title=analysis_input.input.topic,
                    target_reader="블로그 독자",
                    rationale=(
                        "자료 요약 단계가 실패해, 검색으로 찾은 자료만 정리했습니다. "
                        "그대로 진행하거나 '다시 검증'을 눌러 주세요."
                    ),
                    keywords=analysis_input.input.keywords,
                    sources=_sources_for_candidate(
                        [], sources, successful_reference_urls
                    ),
                )
            ],
            collected_source_count=len(sources),
        )

    async def collect_sources(
        self, analysis_input: WebSearchAnalysisInput
    ) -> list[SearchSource]:
        """자료만 다시 모은다 — 방향 후보를 만들지 않는다(2026-08-11 예약 경로).

        새 글 작성에서 방향까지 골라 둔 글이 예약 시각에 원고를 만들 때 쓴다. 방향은 이미
        사람이 골랐으므로 요약(의도 후보 생성) 호출은 통째로 낭비다 — 수집만 부른다.

        **캐시를 쓰지 않는다.** 이 경로의 존재 이유가 신선한 자료인데 10분 캐시가 옛
        수집분을 돌려주면 아무것도 하지 않은 것과 같다. 캐시에 넣지도 않는다 — 예약이
        방금 모은 자료가 곧이어 사용자의 '다시 검증'에 재사용되면 그쪽이 낡은 것을 본다.
        """
        summary, sources, _cacheable, _urls = await self._collect_research(analysis_input)
        side_tasks = []
        if self._blog_research is not None:
            side_tasks.append(("blog", self._collect_naver_blog(analysis_input)))
        if self._news_research is not None:
            side_tasks.append(("news", self._collect_naver_news(analysis_input)))
        if side_tasks:
            results = await asyncio.gather(
                *(coro for _, coro in side_tasks), return_exceptions=True
            )
            for (kind, _), result in zip(side_tasks, results, strict=True):
                merge = self._merged_naver_blog if kind == "blog" else self._merged_naver_news
                summary, sources = merge(summary, sources, [result])
        return sources

    async def search_and_analyze(
        self,
        analysis_input: WebSearchAnalysisInput,
        on_collected: OnResearchCollected | None = None,
        on_note: OnResearchNote | None = None,
    ) -> IntentValidationResult:
        # **자료를 모으지 않는 경우가 있다**(2026-08-12 사용자 결정). 여러 편을 만들거나
        # 작업 시각을 정해 둔 글은 원고를 만들 때 자료를 새로 모으므로, 여기서 미리 모아 봐야
        # 그때 버려진다 — 1~2분과 검색 비용만 쓴다. 방향 후보는 그대로 만든다.
        if not analysis_input.collect_sources:
            if on_collected:
                await on_collected()
            with perf.span("verification_llm") as meta:
                result = await self._summarize_intent(
                    analysis_input, "", [], [], sources_pending=True
                )
                meta["candidates"] = len(result.intent_candidates)
            return result

        # 같은 입력의 재검증이면 몇 분 안의 수집분을 재사용한다. 요약은 캐시하지 않는다 —
        # '다시 검증'을 누른 사용자는 다른 의도 후보를 기대할 수 있다.
        cache_key = self._research_cache_key(analysis_input)
        cached = self._research_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            summary, sources, successful_reference_urls = cached[1], cached[2], cached[3]
            with perf.span("web_research", cache_hit=True) as meta:
                meta["sources"] = len(sources)
            logger.info(
                "M3 수집 캐시 적중 - 자료 %d건 재사용 (남은 TTL %.0f초)",
                len(sources),
                cached[0] - now,
            )
        else:
            # 네이버 블로그 보강은 구글 수집과 병렬로 돈다 — 서로를 기다릴 이유가 없고,
            # 보강 쪽(검색 1회 + 본문 3회)이 대개 먼저 끝난다. 결과는 브리핑·출처에 합쳐
            # 캐시에도 함께 실리므로, '다시 검증'은 블로그를 다시 훑지 않는다.
            blog_task = (
                asyncio.create_task(self._collect_naver_blog(analysis_input))
                if self._blog_research is not None
                else None
            )
            # 최신 기사 보강도 같은 이유로 병렬이다(2026-08-11). 블로그와 서로 다른 것을
            # 묻는다 — 블로그는 '써 보니 어땠나', 뉴스는 '지금 무슨 일이 있었나'.
            news_task = (
                asyncio.create_task(self._collect_naver_news(analysis_input))
                if self._news_research is not None
                else None
            )
            side_tasks = [task for task in (blog_task, news_task) if task is not None]
            try:
                with perf.span("web_research", cache_hit=False) as meta:
                    summary, sources, cacheable, successful_reference_urls = (
                        await self._collect_research(analysis_input)
                    )
                    meta["sources"] = len(sources)
            except BaseException:
                for task in side_tasks:
                    task.cancel()
                if side_tasks:
                    await asyncio.gather(*side_tasks, return_exceptions=True)
                raise
            if blog_task is not None:
                before = len(sources)
                summary, sources = self._merged_naver_blog(
                    summary,
                    sources,
                    await asyncio.gather(blog_task, return_exceptions=True),
                )
                await _note(on_note, len(sources) - before, "네이버 블로그 실사용 글")
            if news_task is not None:
                before = len(sources)
                summary, sources = self._merged_naver_news(
                    summary,
                    sources,
                    await asyncio.gather(news_task, return_exceptions=True),
                )
                await _note(on_note, len(sources) - before, "관련 최신 기사")
            if sources:
                await _say(on_note, f"자료 {len(sources)}건을 모았습니다.")
            if sources and cacheable:
                if len(self._research_cache) >= _RESEARCH_CACHE_MAX_ENTRIES:
                    self._research_cache.pop(next(iter(self._research_cache)))
                self._research_cache[cache_key] = (
                    now + _RESEARCH_CACHE_TTL_SECONDS,
                    summary,
                    sources,
                    successful_reference_urls,
                )

        if on_collected:
            await on_collected()
        try:
            with perf.span("verification_llm") as meta:
                result = await self._summarize_intent(
                    analysis_input, summary, sources, successful_reference_urls
                )
                meta["candidates"] = len(result.intent_candidates)
            return result
        except Exception as error:
            # 요약만 실패한 경우까지 검색 결과를 버리지 않는다. 찾은 자료가 하나도 없을
            # 때만 실패를 그대로 올린다 — 그건 정말로 보여줄 것이 없다는 뜻이다.
            if not sources:
                raise
            logger.warning("M3 의도 요약 실패 - 수집한 자료 %d건으로 대체합니다: %s", len(sources), error)
            return self._sources_only_result(
                analysis_input, sources, successful_reference_urls
            )


class GeminiSiteReader:
    """브랜드의 **자기 사이트**를 읽어 자료로 바꾼다(2026-08-20 사용자 결정).

    M3(`GeminiResearchAnalyzer`)와 같은 두 걸음이고, 같은 이유로 나눠 두었다: 읽는
    호출은 grounding·url_context가 필요하고, 정리하는 호출은 JSON 스키마에 묶여야 한다.

    **M3와 나눠 둔 이유**는 하는 일이 다르기 때문이다. M3는 "이 소재로 무슨 글을 쓸까"를
    가르려고 웹 전체를 훑는다. 여기는 **주어진 사이트가 말하는 것만** 옮긴다 — 검색으로
    찾은 남의 페이지가 섞이면 그 회사가 하지 않는 말이 자기 브랜드 자료로 저장된다.

    자격 증명은 M3와 같은 것을 쓴다. 이것 하나를 위해 새 역할·새 환경변수를 만들면,
    키를 하나 더 넣지 않았다는 이유로 이 기능만 조용히 죽는다.
    """

    def __init__(self, collect_role: RoleConfig, summary_role: RoleConfig):
        self._collect_role = collect_role
        self._summary_role = summary_role
        self._collect_key = _required_api_key(collect_role)
        self._summary_key = _required_api_key(summary_role)

    async def _read(self, site_input: SiteReadInput) -> tuple[str, list[str], list[str]]:
        """사이트를 훑는다 — (읽어 온 글, 읽힌 주소, 못 읽은 주소).

        붙여넣은 글만 있고 주소가 없으면 **모델을 부르지 않는다.** 이미 글자가 있는데
        한 번 더 훑을 이유가 없고, 로그인 뒤 공지를 복사해 온 경우가 바로 그렇다.
        """
        urls = [url for url in dict.fromkeys(site_input.urls) if is_public_reference_url(url)]
        pasted = site_input.text.strip()
        if not urls:
            if not pasted:
                raise LiveAdapterError("읽을 주소도, 붙여넣은 글도 없습니다")
            return pasted, [], []

        payload = await asyncio.wait_for(
            _post_json(
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                {"x-goog-api-key": self._collect_key},
                {
                    "model": self._collect_role.model,
                    "input": prompts.site_collect_prompt(site_input.brand_name, urls),
                    "system_instruction": prompts.SITE_READ_SYSTEM_PROMPT,
                    # **google_search를 켜지 않는다.** 이 단계에서 검색이 붙으면 남의
                    # 페이지가 섞이고, 그 회사가 하지 않는 말이 자기 브랜드 자료로 저장된다.
                    "tools": [{"type": "url_context"}],
                    "store": False,
                    "thinking_config": {"thinking_level": "low"},
                },
            ),
            timeout=RESEARCH_COLLECT_TIMEOUT,
        )
        text = extract_gemini_interaction_text(payload)
        results = extract_gemini_url_context_results(payload)
        read = [
            (result.requested_url or result.url)
            for result in results
            if result.status == "success"
        ]
        failed = [url for url in urls if url not in read]
        if failed:
            logger.info("사이트 읽기 | 요청 %d건 | 못 읽음 %d건", len(urls), len(failed))
        if pasted:
            # 붙여넣은 글은 **뒤에** 붙인다. 사람이 직접 준 것이라 사이트보다 새로울 수
            # 있고(공지가 그렇다), 뒤에 두면 정리 단계가 최신으로 읽는다.
            gap = "\n\n"
            text = f"{text}{gap}[사용자가 붙여넣은 내용]\n{pasted}"
        return text, read, failed

    async def _structure(self, prompt: str, schema: dict) -> dict:
        payload = await _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._summary_role.model}:generateContent",
            {"x-goog-api-key": self._summary_key},
            {
                "systemInstruction": {"parts": [{"text": prompts.SITE_READ_SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": schema,
                    # 이미 읽어 온 글을 칸에 나누는 일이다. 길게 숙고할 것이 없다.
                    "thinkingConfig": {"thinkingLevel": "low"},
                },
            },
        )
        return extract_json_object(extract_gemini_text(payload))

    async def read_feature(self, site_input: SiteReadInput) -> FeatureBrief:
        research, read, _failed = await self._read(site_input)
        parsed = await self._structure(
            prompts.feature_brief_prompt(site_input.brand_name, research),
            GEMINI_FEATURE_BRIEF_SCHEMA,
        )
        name = string_value(parsed.get("name")).strip()
        if not name:
            # 이름을 못 찾으면 글의 소재가 없다. 빈 소재로 글을 만들면 브랜드 이름이 그
            # 자리를 채워(`with_brand_materials`) 신기능 글이 아니라 회사 소개가 된다.
            raise LiveAdapterError("이 페이지에서 기능 이름을 찾지 못했습니다")
        return FeatureBrief(
            name=name,
            summary=string_value(parsed.get("summary")).strip(),
            highlights=string_array(parsed.get("highlights")),
            keywords=string_array(parsed.get("keywords")),
            read_urls=read,
        )


class AnthropicTopicGenerator:
    """M2 제목. 키워드 하나를 넣으면 후킹이 서로 다른 제목 다섯 개가 나온다."""

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)

    async def generate_topics(
        self, topic_input: TopicGenerationInput
    ) -> TopicRecommendationResult:
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m2-topic",
            system=prompts.TOPIC_SYSTEM_PROMPT,
            content=prompts.topic_prompt(topic_input),
            tool_name=TOPIC_TOOL_NAME,
            tool_description="Return the Korean blog title candidates. Do not explain them.",
            tool_schema=TOPIC_SCHEMA,
        )

        parsed = extract_anthropic_tool_input(payload, TOPIC_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))

        raw = parsed.get("topicCandidates")
        raw = raw if isinstance(raw, list) else []

        keyword_id = topic_input.trend_keyword.trend_keyword_id
        candidates: list[TopicCandidate] = []
        for index, item in enumerate(raw[:TOPIC_CANDIDATE_COUNT]):
            source = item if isinstance(item, dict) else {}
            title = string_value(source.get("title")).strip()
            if not title:
                continue
            # 후킹 유형·강도는 화면에 표시하지 않지만, 선택된 제목의 약속을 원고(M4)에 넘기고
            # 채점 근거로 쓰기 위해 보존한다. 모델이 목록 밖 값을 내면 조용히 None으로 둔다
            # (지어낸 후킹을 강제로 유효한 값처럼 취급하지 않는다).
            hook_type = string_value(source.get("hookType")).strip().upper()
            hook_type = hook_type if hook_type in TITLE_HOOK_TYPES else None
            hook_strength = string_value(source.get("hookStrength")).strip().upper()
            hook_strength = hook_strength if hook_strength in TITLE_HOOK_STRENGTHS else None
            candidates.append(
                TopicCandidate(
                    topic_candidate_id=f"topic_{keyword_id}_{index + 1}",
                    title=title,
                    # titleType(기본 유형)을 화면 설명으로 쓴다. 없으면 예전처럼 일반 라벨.
                    description=string_value(source.get("titleType"), "제목 후보"),
                    # 모든 제목은 사용자가 선택한 그 키워드 하나에 대한 것이다.
                    trend_keyword_ids=[keyword_id],
                    # 추천은 여기서 정하지 않는다 — 뒤이은 루브릭 채점(score_titles)이 최고점
                    # 하나에 표시한다. index==0을 추천하던 임의 기준을 없앤다.
                    recommended=False,
                    hook_type=TitleHookType(hook_type) if hook_type else None,
                    hook_strength=TitleHookStrength(hook_strength) if hook_strength else None,
                )
            )

        if not candidates:
            raise LiveAdapterError("Anthropic did not return any title candidate")

        return TopicRecommendationResult(topic_candidates=candidates, generated_at=_now())


class AnthropicDraftGenerator:
    """M4. tool 호출을 강제해 모델이 산문이 아니라 구조화된 데이터를 돌려주게 한다."""

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)

    @staticmethod
    def _message_content(
        prompt: str,
        materials: list[ReferenceMaterial],
        *,
        max_images: int | None = None,
    ) -> str | list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        attached_images = 0
        for material in materials:
            if material.type.value != "IMAGE":
                continue
            if max_images is not None and attached_images >= max_images:
                break
            attachment = prompts.split_data_url(material.value)
            if attachment is None:
                continue
            _declared_mime, data = attachment
            # data URL이 선언한 mime을 믿지 않는다. 브라우저·변환 과정에서 실제 형식과 다른
            # mime이 붙어 오면(예: image/jpeg로 온 WebP) Anthropic이 400으로 거절해 원고 생성이
            # 통째로 죽는다(실측 사례). 실제 바이트로 형식을 판별해 네이티브 형식은 그대로,
            # 그 외는 PNG로 변환해 붙인다. 못 여는 이미지는 건너뛴다(생성은 계속).
            prepared = imaging.prepare_anthropic_image(data)
            if prepared is None:
                continue
            media_type, encoded = prepared
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": encoded},
                }
            )
            attached_images += 1
        return content if len(content) > 1 else prompt

    async def generate_threads_post(self, task, article_length: str | None = None) -> list[str]:
        """하나의 소재를 **스레드 문법으로 새로 써서** 여러 스레드로 돌려준다.

        블로그 원고(generate_draft)와 프롬프트를 분리한다 — SEO·소제목·썸네일 규칙이
        스레드 글에 섞이면 안 된다. 근거는 완성된 블로그 본문에서 취하고, 결과는 개수·
        한도 검증(threads_post_from_json)을 지나야 발행 경로로 넘어간다.

        ``article_length``가 스레드 개수·글자 수·역할을 정한다(짧게 2~3 / 중간 3~5).
        모델이 스스로 개수를 고르지 않는다.
        """
        from . import threads_prompts

        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-threads-post",
            system=threads_prompts.THREADS_POST_SYSTEM_PROMPT,
            content=threads_prompts.threads_post_prompt(task, article_length),
            tool_name=threads_prompts.THREADS_POST_TOOL_NAME,
            tool_description=(
                "Return the ordered Korean Threads posts as structured data. "
                "Do not explain the result."
            ),
            tool_schema=threads_prompts.THREADS_POST_SCHEMA,
        )
        tool_input = extract_anthropic_tool_input(
            payload, threads_prompts.THREADS_POST_TOOL_NAME
        )
        if tool_input is None:
            tool_input = extract_json_object(extract_anthropic_text(payload))
        return threads_prompts.threads_post_from_json(tool_input, article_length)

    async def _complete_json(
        self, system: str, prompt: str, materials: list[ReferenceMaterial] | None = None
    ) -> dict[str, Any]:
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-draft",
            system=system,
            content=self._message_content(prompt, materials or []),
            tool_name=DRAFT_TOOL_NAME,
            tool_description=(
                "Return the completed Korean blog draft as structured data. "
                "Do not explain the result."
            ),
            tool_schema=DRAFT_SCHEMA,
        )

        tool_input = extract_anthropic_tool_input(payload, DRAFT_TOOL_NAME)
        if tool_input is not None:
            return tool_input
        return extract_json_object(extract_anthropic_text(payload))

    async def generate_title_plan(self, draft_input: DraftGenerationInput) -> TitlePlan | None:
        """M4 0단계: 원고보다 먼저 제목을 확정한다.

        설계와 마찬가지로 실패는 여기서 흡수하지 않는다 — 호출부(draft 서비스)가 감싸고,
        제목 계획 없이도 원고는 예전 방식으로 생성된다. 제목은 창작보다 판단에 가까워
        effort를 medium으로 둔다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-title-plan",
            system=prompts.TITLE_PLAN_SYSTEM_PROMPT,
            content=prompts.title_plan_prompt(draft_input),
            tool_name=TITLE_PLAN_TOOL_NAME,
            tool_description=(
                "Return the final title plan as structured data. "
                "Do not write the article."
            ),
            tool_schema=TITLE_PLAN_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, TITLE_PLAN_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        # 사용자가 M2에서 고른 트렌드 제목이 있으면 그 제목이 확정 제목이다. 모델이 다듬어
        # 놓았더라도 코드가 되돌린다 — 사용자가 이미 고른 것이다.
        return title_plan_from_json(parsed, fixed_title=draft_input.trend_title)

    async def generate_reference_evidence(
        self, draft_input: DraftGenerationInput
    ) -> ReferenceEvidenceProfile | None:
        """참고자료를 근거 정보로 바꾸는 단계(원고보다 먼저).

        관찰이지 창작이 아니다 — 여기서 '그럴듯한' 특징을 지어내면 원고와 이미지가 함께
        틀린다. 같은 자료면 같은 판정이 나오는 것이 이상적이지만, 그것을 보장하는 것은
        샘플링 설정이 아니라 고정된 절차·스키마·캐시다(Opus 5는 temperature를 받지 않는다).
        참고 이미지는 실제로 첨부해 보낸다(이름만으로는 무엇이 보이는지 알 수 없다).
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-reference-evidence",
            system=prompts.REFERENCE_EVIDENCE_SYSTEM_PROMPT,
            content=self._message_content(
                prompts.reference_evidence_prompt(draft_input),
                draft_input.input.reference_materials,
            ),
            tool_name=REFERENCE_EVIDENCE_TOOL_NAME,
            tool_description=(
                "Return what the reference material actually confirms, and what "
                "it does not. Do not write the article."
            ),
            tool_schema=REFERENCE_EVIDENCE_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, REFERENCE_EVIDENCE_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        # 원본 검색어는 모델에 되묻지 않는다 — 사용자가 무엇을 골랐는지는 이미 아는 값이고,
        # 여기를 모델에 맡기면 '글에 쓸 표현'과 다시 뒤섞인다.
        return reference_evidence_profile_from_json(
            parsed, primary_raw_keyword(draft_input)
        )

    async def generate_editorial_style_plan(
        self, draft_input: DraftGenerationInput
    ) -> EditorialStylePlan | None:
        """글의 카테고리·형태를 정하는 단계. 테마·팔레트는 코드가 고르므로 묻지 않는다.

        분류 판단이라 effort를 medium으로 둔다. 같은 소재·목적이면 같은 카테고리가 나오는
        것이 이상적이고, 변형은 난수가 아니라 variation_seed가 만든다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-editorial-style",
            system=prompts.EDITORIAL_STYLE_SYSTEM_PROMPT,
            content=prompts.editorial_style_prompt(draft_input),
            tool_name=EDITORIAL_STYLE_TOOL_NAME,
            tool_description=(
                "Return the article's subject category and editorial form. "
                "Do not write the article."
            ),
            tool_schema=EDITORIAL_STYLE_PLAN_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, EDITORIAL_STYLE_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        return editorial_style_plan_from_json(parsed)

    async def generate_content_plan(
        self, draft_input: DraftGenerationInput
    ) -> ContentPlan | None:
        """M4 1단계: 본문을 쓰기 전의 콘텐츠 설계. 구조와 정보 배치가 원고 품질을 결정하므로
        effort를 high로 둔다.

        실패(형식 오류·API 오류)는 여기서 None으로 흡수하지 않는다 — 호출부(draft 서비스)가
        try/except로 감싸 설계 없이 진행한다. 설계는 품질 장치이지 원고 생성의 관문이 아니다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-content-plan",
            system=prompts.CONTENT_PLAN_SYSTEM_PROMPT,
            content=prompts.content_plan_prompt(draft_input),
            tool_name=CONTENT_PLAN_TOOL_NAME,
            tool_description=(
                "Return the article content plan as structured data. "
                "Do not write the article body."
            ),
            tool_schema=CONTENT_PLAN_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, CONTENT_PLAN_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        return content_plan_from_json(parsed)

    async def generate_seo_keyword_plan(
        self, draft_input: DraftGenerationInput
    ) -> SeoKeywordPlan | None:
        """M4: 원고를 쓰기 전에 만드는 SEO 키워드 계획(primary·secondary·avoid).

        설계·제목 계획과 마찬가지로 실패(형식 오류·API 오류)는 여기서 흡수하지 않는다 —
        호출부(draft 서비스)가 try/except로 감싸 계획 없이 진행한다. 계획은 검색 노출을 돕는
        품질 장치이지 원고 생성의 관문이 아니다. primary는 제목이 노리는 핵심 검색 구문에
        맞춰야 하므로 창작보다 판단에 가깝다 — effort를 medium으로 둔다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-seo-plan",
            system=prompts.SEO_KEYWORD_PLAN_SYSTEM_PROMPT,
            content=prompts.seo_keyword_plan_prompt(draft_input),
            tool_name=SEO_KEYWORD_PLAN_TOOL_NAME,
            tool_description=(
                "Return the SEO keyword plan (primary, secondary, avoid). "
                "Do not write the article."
            ),
            tool_schema=SEO_KEYWORD_PLAN_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, SEO_KEYWORD_PLAN_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        # primary를 확정 제목이 노리는 핵심 검색 구문에 맞춰 고정한다(제목·첫 문단 검증 통과).
        # 제목 계획을 만들지 못한 글에도 사용자가 M2에서 고른 제목은 있으므로 함께 넘긴다.
        return seo_keyword_plan_from_json(
            parsed,
            title_plan=draft_input.title_plan,
            fixed_title=draft_input.trend_title,
        )

    async def generate_draft(self, draft_input: DraftGenerationInput) -> DraftGenerationResult:
        hashtag_count = draft_input.settings.hashtag_count if draft_input.settings else 5
        parsed = await self._complete_json(
            prompts.DRAFT_SYSTEM_PROMPT,
            prompts.draft_prompt(draft_input),
        )
        final_post = final_post_from_json(
            parsed.get("finalPost"),
            # 트렌드를 고른 글이면 모델이 제목을 빠뜨렸을 때의 폴백도 앵커 제목으로
            # 둔다. 트렌드 없는 글은 예전처럼 선택 의도 제목이 폴백이다.
            draft_input.trend_title or draft_input.selected_intent.title,
            hashtag_count,
            prompts.draft_hashtag_seeds(draft_input),
            # 제목이 원고보다 먼저 확정된 글은 모델이 무엇을 반환했든 그 제목을 쓴다.
            forced_title=(
                draft_input.title_plan.primary_title if draft_input.title_plan else None
            ),
        )
        _log_empty_body(parsed.get("finalPost"), final_post, draft_input.post_id)
        return DraftGenerationResult(
            prompt_version=draft_input.prompt_version,
            provider="anthropic",
            model=self._role.model,
            generated_at=_now(),
            final_post=final_post,
            content_plan=draft_input.content_plan,
            # 코드로 렌더링할 시각자료(차트·과정도·인포그래픽)의 구조화 데이터. 수치·출처
            # 검증과 렌더링은 draft 서비스의 visuals 단계가 맡는다.
            visuals=planned_visuals_from_json(parsed.get("visuals")) or None,
        )

    async def generate_visual_card_plan(
        self,
        draft_input: "DraftGenerationInput",
        final_post,
        rendered_visual_count: int,
        reference_image_count: int,
    ):
        """원고 완성 후의 자연 사진 계획. 후보 추출·채점(80점 게이트)은 프롬프트가,
        최대 6장 예산·원고 대조 강제는 draft 서비스가 맡는다.

        실패는 호출부가 try/except로 감싸 계획 없이(기존 방식) 진행한다 — 카드 계획은
        품질 장치이지 원고 생성의 관문이 아니다."""
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-card-plan",
            system=prompts.CARD_PLAN_SYSTEM_PROMPT,
            # 원본 이미지는 앞선 reference-evidence 단계에서 한 번만 읽힌다. 여기서는
            # 개인정보 판정까지 끝난 텍스트 근거 프로필만 보내 중복 업로드·노출·지연을 막는다.
            content=prompts.card_plan_prompt(
                draft_input,
                final_post,
                rendered_visual_count,
                reference_image_count,
            ),
            tool_name=CARD_PLAN_TOOL_NAME,
            tool_description=(
                "Return the natural-photo plan for the finished article. "
                "Include the required thumbnail and only body photos scoring 80 or higher."
            ),
            tool_schema=CARD_PLAN_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, CARD_PLAN_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        return card_plan_from_json(parsed)

    async def review_final_draft(self, draft_input: "DraftGenerationInput", final_post):
        """M4 4단계: 완성 원고를 입력·조사 자료와 대조하고, 고칠 문장까지 함께 받는다.

        참고 이미지를 함께 올리지 않는다. 검수가 보는 것은 '자료가 말하는 것과 원고가
        말하는 것이 같은가'이고, 그 자료는 이미 프롬프트 안에 글로 들어 있다. 여기에
        이미지를 얹으면 회차마다 같은 바이트를 다시 올리게 된다.

        응답이 도구 스키마로 오지 않으면(텍스트로 새거나 JSON이 깨지면) **형식만 고쳐서
        한 번 더 부른다.** 검수 내용은 다시 판단할 필요가 없고 담는 그릇만 틀린 경우라,
        전체를 포기하기 전에 한 번은 되물어 보는 편이 싸다.

        실패는 호출부가 감싸 검수 없이 진행한다 — 이 단계는 마무리이지 관문이 아니다.
        """
        prompt = prompts.final_review_prompt(draft_input, final_post)
        parsed = await self._call_final_review(prompt)
        if parsed is None:
            logger.info("최종 검수 응답 형식이 어긋나 형식 교정으로 한 번 더 부릅니다")
            parsed = await self._call_final_review(
                prompt + "\n\n" + prompts.FINAL_REVIEW_FORMAT_REPAIR
            )
        if parsed is None:
            raise LiveAdapterError("최종 검수 응답을 JSON으로 읽지 못했습니다")

        checks = final_review_checks_from_json(parsed)
        status, score = final_review_overall_from_json(parsed, checks)
        return FinalReviewReport(
            overall_status=status,
            overall_score=score,
            checks=checks,
            issues=final_review_issues_from_json(parsed),
        )

    async def _call_final_review(self, prompt: str) -> dict | None:
        """검수 한 번. 도구 입력으로 못 읽으면 텍스트에서 JSON을 건져 보고, 그것도 아니면 None."""
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-final-review",
            system=prompts.FINAL_REVIEW_SYSTEM_PROMPT,
            content=prompt,
            tool_name=FINAL_REVIEW_TOOL_NAME,
            tool_description=(
                "Judge the finished article on the seven quality checks and return only the "
                "problems that must be fixed, each with the exact sentence to replace."
            ),
            tool_schema=FINAL_REVIEW_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, FINAL_REVIEW_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        return parsed if isinstance(parsed, dict) else None

    async def critique_final_draft(
        self, draft_input: "DraftGenerationInput", final_post, model_markdown: str
    ) -> dict:
        """M4 마무리 1단계: 완성 원고에 대한 **의견**(좋은 점·아쉬운 점·개선점).

        그림은 올리지 않는다(이 계열은 픽셀 없이 대체텍스트·캡션만 본다). 그래서
        프롬프트가 imageFindings를 빈 배열로 두라고 못박는다 — 보지 않은 그림을
        평가하지 않는다. 그림을 실제로 보는 쪽은 2차 검토(OpenAiFinalReviewer)다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-critique",
            system=prompts.CRITIQUE_SYSTEM_PROMPT,
            content=prompts.critique_prompt(
                draft_input, final_post, model_markdown, sees_images=False
            ),
            tool_name=CRITIQUE_TOOL_NAME,
            tool_description=(
                "Return strengths, weaknesses and concrete improvements for the article."
            ),
            tool_schema=CRITIQUE_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, CRITIQUE_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        if not isinstance(parsed, dict):
            raise LiveAdapterError("원고 비평 응답을 JSON으로 읽지 못했습니다")
        return parsed

    async def integrate_critiques(
        self,
        draft_input: "DraftGenerationInput",
        final_post,
        model_markdown: str,
        review_a: str,
        review_b: str | None,
    ) -> dict:
        """M4 마무리 2단계: 두 검토를 통합해 원고를 다시 쓴다.

        검토의 출처는 프롬프트가 가린다(A·B). 하나는 이 모델 자신의 검토인데, 그것을
        알면 자기 검토를 편들게 된다 — 통합을 원고를 쓴 모델에게 맡기면서 그 위험을
        줄이는 장치다(2026-08-07 사용자 논의).

        자리표([[IMAGE:n]]) 검사는 여기서 하지 않는다 — 코드(critique.rebuild_post)가
        하고, 어겼으면 재작성 전체가 버려진다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-integrate",
            system=prompts.INTEGRATION_SYSTEM_PROMPT,
            content=prompts.integration_prompt(
                draft_input, final_post, model_markdown, review_a, review_b
            ),
            tool_name=INTEGRATION_TOOL_NAME,
            tool_description=(
                "Merge the two reviews into decisions and return the improved article"
                " markdown, keeping every [[IMAGE:n]] placeholder exactly once."
            ),
            tool_schema=INTEGRATION_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, INTEGRATION_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        if not isinstance(parsed, dict):
            raise LiveAdapterError("통합 재작성 응답을 JSON으로 읽지 못했습니다")
        return parsed

    async def polish_final_draft(
        self,
        draft_input: "DraftGenerationInput",
        final_post,
        *,
        has_experience_material: bool = False,
    ):
        """M4 5단계: 사실 검수를 마친 원고에서 **문장 표현만** 고칠 자리를 받는다.

        4단계와 나눠 둔 이유는 보는 것이 다르기 때문이다. 검수는 자료와 대조해 '맞는
        말인가'를 보고, 여기서는 '사람이 쓴 블로그 글로 읽히는가'를 본다. 한 호출에 둘을
        같이 시키면 모델은 둘 중 눈에 띄는 쪽(사실)만 보고 표현은 대충 지나간다.

        참고 이미지는 올리지 않는다 — 다듬는 것은 문장이고, 그림은 이 판단에 필요 없다.

        실패는 호출부가 감싸 다듬기 없이 진행한다. 여기 도착한 원고는 이미 검수까지 끝난
        완성본이라, 마무리 한 단계 때문에 결과를 못 받는 쪽이 더 나쁘다.
        """
        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m4-polish",
            system=prompts.POLISH_SYSTEM_PROMPT,
            content=prompts.polish_prompt(
                draft_input, final_post, has_experience_material=has_experience_material
            ),
            tool_name=POLISH_TOOL_NAME,
            tool_description=(
                "Return only the sentences that read awkward or machine-written, each with "
                "the exact sentence to replace and its natural rewrite. Facts must not change."
            ),
            tool_schema=POLISH_SCHEMA,
        )
        parsed = extract_anthropic_tool_input(payload, POLISH_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))
        return polish_edits_from_json(parsed)


def _supports_flexible_image_size(model: str) -> bool:
    """gpt-image-2와 날짜 고정 snapshot만 사용자 지정 16px 배수 규격을 쓴다."""
    normalized = (model or "").strip().lower()
    return normalized == "gpt-image-2" or normalized.startswith("gpt-image-2-")


def _is_size_schema_rejection(error: ProviderRequestError) -> bool:
    """400 중 size 필드만 거절한 경우인가. 프롬프트·moderation 오류는 폴백하지 않는다."""
    if error.status_code != 400:
        return False
    details = error.payload.get("error") if isinstance(error.payload, dict) else None
    if isinstance(details, dict):
        parameter = str(details.get("param") or "").strip().lower()
        if parameter == "size" or parameter.endswith(".size"):
            return True
        message = str(details.get("message") or "").lower()
    else:
        message = error.detail.lower()
    return "size" in message and any(
        marker in message
        for marker in ("invalid", "unsupported", "must be", "one of", "not allowed")
    )


def _web_image_source_url(photo) -> str | None:
    """복사·외부 에디터가 <img src>로 바로 쓸 수 있는 **이미지 주소**(2026-08-10).

    네이버 검색 사진의 source_url은 이미지 파일 주소 그대로다. 유튜브는 source_url이
    영상(watch) 주소라 이미지가 아니므로, 내려받을 때 쓴 썸네일 주소를 video_id로
    되만든다(다운로드가 성공한 사진만 여기 오므로 그 주소는 실재한다). 유도할 수 없으면
    None — 그 이미지는 예전처럼 로컬 엔드포인트로만 나간다.
    """
    if photo is None:
        return None
    if photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL:
        return (
            f"https://i.ytimg.com/vi/{photo.video_id}/maxresdefault.jpg"
            if photo.video_id
            else None
        )
    url = (photo.source_url or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _captioned_with_source(card_caption: str | None, photo) -> str | None:
    """웹에서 가져온 사진의 출처 캡션(2026-08-10 사용자 지시 — 네이버 블로그 이미지
    출처 표기 규칙: 이미지 아래 '출처: 사이트 이름', 원본 주소가 있으면 함께).

    적는 것은 **실제 원본 출처**다(2026-08-11 사용자 지시). 예전에는 이미지 파일이 놓인
    CDN 호스트가 그대로 적혔다 — '출처: imgnews.naver.net', '출처: shop-phinf.pstatic.net'.
    이제 photo_search가 이미지 주소에서 되찾아 둔 사이트/서비스 이름과 원본 페이지 주소를
    쓴다(app.llm.image_origin). 되찾지 못한 사진은 예전처럼 호스트명이 적힌다 — 모르는
    출처를 지어내지 않는다.

    **주소는 괄호로 감싸지 않는다**(2026-08-11 사용자 신고 — "이미지를 클릭하면 페이지를
    찾을 수 없다고 뜬다"). 예전 형식은 `출처: 뉴스1 (https://n.news.naver.com/article/…)`
    였는데, 캡션을 자동으로 링크로 바꾸는 쪽(네이버 에디터·블로그·마크다운 뷰어)이 닫는
    괄호까지 주소에 넣어 버린다. 실측: 끝에 `)`가 붙은 주소는 **500 + "페이지를 찾을 수
    없습니다"**로, 사용자가 본 화면과 정확히 같았다(괄호 없는 같은 주소는 200에 본문도
    있다). 그래서 주소는 캡션 **맨 끝**에 공백으로만 떼어 둔다 — 뒤에 아무 글자도 붙지
    않으면 어떤 자동 링크도 주소를 온전히 가져간다.

    카드가 준비한 설명이 있으면 앞에 둔다(주소가 끝에 남아야 하므로 순서가 중요하다).
    생성·렌더링 이미지는 가져온 것이 아니므로 출처를 붙이지 않는다.
    """
    if photo is None:
        return card_caption
    if photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL and photo.channel_title:
        name, page_url = f"YouTube {photo.channel_title}", (photo.source_url or "").strip()
    else:
        # 검색 단계가 확인해 둔 사이트 이름이 먼저다(2026-08-11). 확인하지 못했으면
        # 예전처럼 호스트를 적는다 — 표기할 수 있는 가장 정확한 것이 여전히 그것이다.
        name = photo.source_name or photo.source_host
        # 원문 페이지를 **확인한** 사진만 주소가 있다(네이버 뉴스 기사·유튜브 영상).
        page_url = (getattr(photo, "source_page_url", "") or "").strip()
    source = f"출처: {name} — {page_url}" if page_url else f"출처: {name}"
    return f"{card_caption} · {source}" if card_caption else source


class OpenAiPostImageGenerator:
    """M5.

    모델은 규격이 요구하는 크기를 하나도 그리지 못하고 한글도 못 쓴다. 그래서 받아 오는
    것은 원본 사진이고 — 규격(썸네일 720×720, 본문 900×506)으로 맞추는 것도, 한글 문구를
    얹는 것도 여기 app.llm.imaging에서 한다.

    소재가 실존 인물이면 web_photo가 실려 온다. 그때는 생성 호출을 건너뛰고 그 사진을
    결과로 쓴다 — 이름을 아무리 강하게 요구해도 모델은 그 사람을 그리지 못한다.
    """

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)
        # 공식 gpt-image-2는 1200×688을 받는다. 다만 오래된 프록시/API schema가 표준
        # 세 규격만 허용하는 배포에서는 첫 400 뒤 이 인스턴스의 후속 카드도 표준 크기를 쓴다.
        self._flexible_size_available = _supports_flexible_image_size(role.model)

    async def _request_image(
        self,
        prompt: str,
        size: str,
        quality: str,
        reference_pngs: list[bytes],
    ) -> Any:
        """생성·편집 요청의 공통 옵션. 응답 형식도 JPEG로 맞춰 전송량을 줄인다."""
        if reference_pngs:
            return await _post_multipart(
                "https://api.openai.com/v1/images/edits",
                {"authorization": f"Bearer {self._api_key}"},
                {
                    "model": self._role.model,
                    "size": size,
                    "prompt": prompt,
                    "quality": quality,
                    "output_format": "jpeg",
                    "output_compression": "90",
                    "moderation": "auto",
                },
                # 여러 장을 보낼 때는 image[]로 보낸다(같은 대상의 다른 각도). 첫 장이
                # 주 편집 기준이고 나머지는 같은 사람이라는 근거로 함께 읽힌다.
                (
                    {"image": ("reference.png", reference_pngs[0], "image/png")}
                    if len(reference_pngs) == 1
                    else [
                        ("image[]", (f"reference-{index}.png", png, "image/png"))
                        for index, png in enumerate(reference_pngs, start=1)
                    ]
                ),
                model=self._role.model,
            )
        return await _post_json(
            "https://api.openai.com/v1/images/generations",
            {"authorization": f"Bearer {self._api_key}"},
            {
                "model": self._role.model,
                "size": size,
                "prompt": prompt,
                "quality": quality,
                "output_format": "jpeg",
                "output_compression": 90,
                "moderation": "auto",
            },
        )

    async def generate_post_image(
        self, image_input: PostImageGenerationInput
    ) -> GeneratedPostImage:
        prompt = prompts.image_prompt(image_input)
        if image_input.is_thumbnail:
            size = COVER_IMAGE_SIZE
            quality = COVER_IMAGE_QUALITY
        else:
            size = (
                BODY_IMAGE_SIZE
                if self._flexible_size_available
                else LEGACY_LANDSCAPE_IMAGE_SIZE
            )
            quality = BODY_IMAGE_QUALITY

        # 웹에서 찾아온 실제 사진이 실려 있으면 이미지 모델을 부르지 않는다. 실존 인물은
        # 아무리 강하게 지시해도 모델이 '닮은 남'을 그리므로, 그 사람이 보여야 하는 자리에는
        # 생성물이 아니라 사진이 들어가야 한다. 규격 맞추기·문구 얹기는 아래 공통 경로가
        # 그대로 한다 — 여기서는 받아 오는 바이트의 출처만 다르다.
        photo = image_input.web_photo
        if photo is not None:
            is_user_reference = photo.source_host == "user-reference"
            attachment = prompts.split_data_url(photo.data_url)
            if attachment is None:
                if is_user_reference:
                    raise UnsafeImageError("사용자 참고 이미지를 안전하게 열 수 없습니다")
                logger.warning(
                    "웹 사진을 열 수 없어 생성으로 되돌린다 | %s - %s",
                    image_input.post_id,
                    photo.source_url,
                )
            else:
                try:
                    raw_photo = base64.b64decode(attachment[1], validate=True)
                    return await self._image_from_bytes(
                        image_input, raw_photo, prompt, photo
                    )
                except (binascii.Error, UnsafeImageError) as error:
                    if is_user_reference:
                        # REUSED는 로고·패키지·정확한 문구를 원본 그대로 쓰라는 계약이다.
                        # 원본을 못 열었다고 임의의 생성 이미지로 바꾸면 가장 위험한 왜곡이 된다.
                        raise UnsafeImageError(
                            "사용자 참고 이미지의 안전 검사가 실패했습니다"
                        ) from error
                    logger.warning(
                        "웹 사진이 안전 검사를 통과하지 못해 생성으로 되돌린다 | %s - %s: %s",
                        image_input.post_id,
                        photo.source_url,
                        error,
                    )

        # 참고 이미지가 실려 있으면 image-to-image(편집)로 그 이미지를 시각 기준으로 삼아
        # 장면을 생성한다 — 생성 결과가 참고 이미지를 닮게 한다. 참고 이미지를 열 수 없으면
        # None이 되어 아래 일반 텍스트→이미지 생성으로 되돌아간다(생성을 막지 않는다).
        #
        # 실존 인물·캐릭터 카드는 인물 확인용 참고 이미지(reference_person_images)도 함께
        # 보낸다. 이름만으로는 그 사람의 얼굴이 재현되지 않으므로, 얼굴의 근거가 되는 사진이
        # 있으면 그것이 편집 기준이 되어야 한다 — URL만 들고 있고 모델에는 안 보내면
        # 정체성은 여전히 텍스트뿐이다.
        edit_sources = [
            url
            for url in (
                [image_input.reference_image] if image_input.reference_image else []
            )
            + list(image_input.reference_person_images)
            if url
        ]
        reference_pngs: list[bytes] = []
        for url in edit_sources:
            attachment = prompts.split_data_url(url)
            if attachment is None:
                continue
            png = imaging.to_edit_input_png(attachment[1])
            if png is not None and png not in reference_pngs:
                reference_pngs.append(png)

        try:
            payload = await self._request_image(prompt, size, quality, reference_pngs)
        except ProviderRequestError as error:
            if size != BODY_IMAGE_SIZE or not _is_size_schema_rejection(error):
                raise
            logger.warning(
                "provider가 gpt-image-2 사용자 지정 크기 %s를 거부해 %s로 폴백합니다 | %s",
                size,
                LEGACY_LANDSCAPE_IMAGE_SIZE,
                image_input.post_id,
            )
            self._flexible_size_available = False
            payload = await self._request_image(
                prompt, LEGACY_LANDSCAPE_IMAGE_SIZE, quality, reference_pngs
            )
        try:
            raw = base64.b64decode(
                extract_openai_image_base64(payload), validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise LiveAdapterError("OpenAI image response contained invalid base64") from error
        return await self._image_from_bytes(image_input, raw, prompt, None)

    async def _image_from_bytes(
        self,
        image_input: PostImageGenerationInput,
        raw: bytes,
        prompt: str,
        photo: WebPhoto | None,
    ) -> GeneratedPostImage:
        """받아 온 사진 바이트를 원고에 실을 이미지로 만든다.

        생성 결과든 웹에서 찾아온 실제 사진이든 그 뒤 처리는 같다: 썸네일은 배치 계획대로
        한글 문구를 얹고, 본문 사진은 규격(900×506)만 맞춘다. ``photo``가 있으면 출처를
        기록하고 캡션으로 밝힌다 — 가져온 사진이 출처 없이 실리는 경로는 두지 않는다.
        """
        card = image_input.card
        content_prompt = (image_input.content_prompt or "").strip()
        title = image_input.final_post.title

        layout = image_input.thumbnail_layout
        # 배치 계획이 문구를 끄면(NO_COPY_EDITORIAL_PHOTO 등) 글자를 얹지 않는다.
        # 문구 없는 썸네일은 정상 결과이므로 제목을 억지로 다시 넣지 않는다.
        copy_lines = (
            []
            if layout is not None and not layout.show_copy
            else imaging.thumbnail_lines(image_input.thumbnail_copy, title)
        )
        keyword_colors = (
            imaging.thumbnail_keyword_colors(
                copy_lines,
                topic=image_input.input.topic,
                subject=image_input.input.subject,
                keywords=list(image_input.input.keywords),
                intent_keywords=list(image_input.selected_intent.keywords),
                subject_identity=image_input.subject_identity,
                emphasis_words=list(card.emphasis_words) if card is not None else [],
                accent_family=image_input.thumbnail_accent_family,
                accent_color=(
                    image_input.design.accent_color
                    if image_input.design is not None
                    else None
                ),
            )
            if image_input.is_thumbnail
            else {}
        )
        if keyword_colors:
            logger.info(
                "썸네일 핵심어 색상 적용 | %s - %s",
                image_input.post_id,
                ", ".join(keyword_colors),
            )

        # 자를 때는 가운데다 — 프롬프트가 피사체를 가운데 두라고 요구한다.
        #
        # 위쪽을 남기는 크롭(imaging.FACE_CROP)은 더 이상 쓰지 않는다(2026-08-13).
        # 그것은 '세로로 긴 보도 사진을 자를 때 얼굴을 지키는' 장치였는데, 아래에서 웹
        # 사진을 아예 자르지 않기로 하면서 지킬 얼굴이 잘릴 일이 없어졌다.
        crop_bias = imaging.CENTER_CROP
        # **웹에서 찾아온 사진은 자르지 않는다**(2026-08-13 사용자 지시: "워터마크가
        # 짤리잖아. 안짤리게 해서 삽입하고 싶어").
        #
        # 우리가 만든 이미지와 남의 사진은 다루는 규칙이 달라야 한다. 남의 사진에는
        # 언론사 로고·워터마크·경기장 배경 문구가 **프레임 가장자리에** 구워져 있고,
        # 규격을 맞추느라 자르면 그것이 잘려 나간다 — 출처를 지우는 모양이 되고, 화면에는
        # 글자가 반쯤 잘린 채로 남는다.
        #
        # 예전에는 이 보존을 세 가지 조건으로 좁게 걸었다: 공식 유튜브 썸네일이거나,
        # (인물이 아니면서 전체 형태를 보여야 하는 사진이) 35%보다 많이 잘릴 때만.
        # 그래서 인물이 든 보도 사진은 언제나 잘렸다 — 사용자가 본 것이 그 경우다.
        # 이제 웹 사진이면 조건 없이 보존한다.
        #
        # 남는 자리는 검은 띠가 아니라 같은 사진을 흐리게 깐 배경이라(_blurred_backdrop)
        # 한 장의 사진으로 읽힌다. 16:9 원본은 본문 규격에 여백 없이 그대로 들어가므로
        # 대부분의 보도 사진은 결과가 예전과 같다.
        #
        # 생성 이미지는 예전 그대로 자른다. 우리가 요청한 3:2로 오고 프롬프트가 피사체를
        # 가운데 두라고 요구하므로, 규격에 맞추는 크롭이 잃는 것이 없다.
        contain = photo is not None
        # contain이면 자르지 않으므로 크롭 상한은 볼 일이 없다. 생성 이미지에는 예전처럼
        # 걸지 않는다(모델이 다른 비율을 낸 날에만 조용히 그림이 달라진다).
        max_crop_loss = None

        if card is not None:
            # 저장 호환상 card이지만 결과는 자연 사진이다. 썸네일에만 FinalPost의 짧은
            # 문구를 배치 계획이 정한 영역에 합성하고, 본문 사진은 텍스트 없이 16:9로 자른다.
            if image_input.is_thumbnail:
                rendered = await asyncio.to_thread(
                    imaging.render_thumbnail,
                    raw,
                    copy_lines,
                    layout,
                    keyword_colors,
                    crop_bias,
                    contain,
                    max_crop_loss,
                )
            else:
                rendered = await asyncio.to_thread(
                    imaging.to_canvas, raw, crop_bias, contain, max_crop_loss
                )
            alt_text = card.alt_text or image_input.content_alt or (
                f"{title} 대표 썸네일" if image_input.is_thumbnail else f"{title} 본문 이미지"
            )
        elif image_input.is_thumbnail:
            # PIL 렌더링은 CPU 작업이라 이벤트 루프를 막는다 — 이미지 여러 장이 병렬로
            # 도는 동안 루프가 서야 할 이유가 없다.
            rendered = await asyncio.to_thread(
                imaging.render_thumbnail,
                raw,
                copy_lines,
                layout,
                keyword_colors,
                crop_bias,
                contain,
                max_crop_loss,
            )
            alt_text = (
                f"{title} 대표 썸네일: {' '.join(copy_lines)}"
                if copy_lines
                else f"{title} 대표 썸네일"
            )
        else:
            rendered = await asyncio.to_thread(
                imaging.to_canvas, raw, crop_bias, contain, max_crop_loss
            )
            # 태그의 alt= 필드(한국어)가 우선이다. 없으면(alt 필드 도입 전 원고) 영어
            # 장면 묘사가 그나마 이 이미지를 구체적으로 설명하므로 그대로 쓴다.
            alt_text = (
                image_input.content_alt or content_prompt or f"{title} 본문 이미지"
            )

        encoded = base64.b64encode(rendered).decode("ascii")

        return GeneratedPostImage(
            data_url=f"data:image/jpeg;base64,{encoded}",
            alt_text=alt_text,
            # 웹 사진에는 생성 프롬프트를 남기지 않는다 — 이 사진은 그 프롬프트로 만들어진
            # 것이 아니므로, 그대로 두면 기록이 거짓말을 한다.
            prompt=(
                (
                    f"공식 영상 썸네일(질의 '{photo.query}', 영상 {photo.source_url}"
                    f", 채널 {photo.channel_title}) — 생성하지 않음"
                    if photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
                    else f"웹 검색 사진(질의 '{photo.query}', 출처 {photo.source_url}) — 생성하지 않음"
                )
                if photo is not None
                else prompt
            ),
            provider="web-photo" if photo is not None else "openai",
            model=photo.source_host if photo is not None else self._role.model,
            generated_at=_now(),
            mime_type="image/jpeg",
            source="web" if photo is not None else "generated",
            # 웹에서 가져온 사진은 출처를 캡션으로 함께 싣는다(2026-08-10 사용자 지시 —
            # 네이버 이미지 출처 표기 규칙 준수. 2026-08-03의 '자동 표기 안 함' 결정을
            # 대체한다). 캡션은 네이버 발행 시 사진 캡션 필드에 그대로 들어간다.
            caption=_captioned_with_source(
                card.caption if card is not None else None, photo
            ),
            # 원본 이미지 주소 — 원고 복사가 로컬 주소 대신 이것을 쓴다(밖에서도 보인다).
            source_url=_web_image_source_url(photo),
            # 구조화된 출처(2026-08-11). caption과 같은 사실을 값으로 들고 나간다 —
            # 화면이 원문 링크·이용 조건까지 그리려면 문자열 하나로는 되지 않는다.
            # 가져온 사진은 external, 모델이 그린 것은 generated로 서로 다르게 다룬다.
            image_source=(
                image_origin.web_photo_image_source(
                    photo, original_image_url=_web_image_source_url(photo)
                )
                if photo is not None
                else image_origin.generated_image_source()
            ),
        )


class AnthropicKeywordRanker:
    """수집한 각 키워드가 사용자의 소재와 얼마나 잘 엮이는지 점수를 매긴다.

    뜨는 것과 관련 있는 것은 다르다. Google은 온 국민이 검색하는 것을 알려주므로, 패널이
    AIONA를 쓰는 사람에게 참교육을 추천했다 — 뜨겁지만 쓸모없다. 어떤 키워드가 바로 이 글에
    엮일 수 있는지는 의미에 대한 판단이고, 그게 모델이 하는 일이다.

    제목을 쓰는 것과 같은 M2 역할에서 돈다: 같은 종류의 편집 판단이고, 두 번째 모델은
    유지할 것이 하나 더 늘어나는 일이다.
    """

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)

    async def rank_keywords(
        self, relevance_input: KeywordRelevanceInput
    ) -> dict[str, KeywordJudgment]:
        if not relevance_input.keywords:
            return {}

        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m2-keyword-relevance",
            system=prompts.RELEVANCE_SYSTEM_PROMPT,
            content=prompts.keyword_relevance_prompt(relevance_input),
            tool_name=RELEVANCE_TOOL_NAME,
            tool_description="Score every keyword. Do not explain the scores.",
            tool_schema=RELEVANCE_SCHEMA,
        )

        parsed = extract_anthropic_tool_input(payload, RELEVANCE_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))

        raw = parsed.get("keywords")
        raw = raw if isinstance(raw, list) else []

        def score_or_none(value: object) -> float | None:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, min(100.0, float(value)))
            return None

        judgments: dict[str, KeywordJudgment] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            keyword = string_value(item.get("keyword")).strip()
            relevance = item.get("relevance")
            if keyword and isinstance(relevance, (int, float)) and not isinstance(relevance, bool):
                # 목록에 없는 분야는 신뢰하지 않고 none으로 둔다 — 다양성 단계는 아는 분야만
                # 묶는다.
                category = string_value(item.get("category")).strip()
                relation = string_value(item.get("relationType")).strip().upper()
                relation = relation if relation in RELATION_TYPES else None
                # 관계 유형이 정한 상한을 코드에서 강제한다. 모델이 유형과 점수를 어긋나게
                # 내도(예: NONE인데 subjectRelevance 80) 규격대로 깎인다.
                subject, purpose, capped_relevance = _capped_by_relation(
                    relation,
                    score_or_none(item.get("subjectRelevance")),
                    score_or_none(item.get("purposeRelevance")),
                    max(0.0, min(100.0, float(relevance))),
                )
                judgments[keyword] = KeywordJudgment(
                    relevance=capped_relevance,
                    category=category if category in TREND_CATEGORIES else None,
                    # 관계 유형을 판정에 실어 보낸다. 상한 계산에만 쓰고 버리면 소재 관련순의
                    # 게이트가 점수 하나에만 기대게 되고, 같은 50점인 CONTEXTUAL과 FORCED를
                    # 구분할 수 없다.
                    relation_type=RelationType(relation) if relation else None,
                    # 부분 점수. 모델이 빠뜨리면 None으로 두어 '판정 없음'과 '낮음'을
                    # 구분할 수 있게 한다. blendability는 이전 응답 역호환용으로만 읽는다.
                    subject_relevance=subject,
                    purpose_relevance=purpose,
                    persona_relevance=score_or_none(item.get("personaRelevance")),
                    # 결합 가능성에는 관계 상한을 씌우지 않는다(_capped_by_relation 참고).
                    blendability=score_or_none(item.get("blendability")),
                )

        if not judgments:
            raise LiveAdapterError("Anthropic did not score any keyword")
        return judgments


class AnthropicTopicEvaluator:
    """생성된 제목들을 루브릭의 의미 판단 항으로 채점한다(관련성·트렌드 반영·목적 부합·독자 관심).

    생성과 평가를 분리한다: 제목을 쓰는 호출과 별개로, 이미 나온 제목들을 한 번의 배치 호출로
    평가한다. 제목을 쓰는 것과 같은 M2 역할에서 돈다 — 같은 종류의 편집 판단이다. 완성도(길이·낚시)는
    코드가 규칙으로 결정하므로 여기서 묻지 않는다.
    """

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)

    async def evaluate_titles(
        self, evaluation_input: TitleEvaluationInput
    ) -> dict[str, TitleJudgment]:
        if not evaluation_input.titles:
            return {}

        payload = await anthropic_tool_call(
            api_key=self._api_key,
            model=self._role.model,
            stage="m2-title-eval",
            system=prompts.TITLE_EVAL_SYSTEM_PROMPT,
            content=prompts.title_evaluation_prompt(evaluation_input),
            tool_name=TITLE_EVALUATION_TOOL_NAME,
            tool_description="Score every title on the rubric. Do not rewrite the titles.",
            tool_schema=TITLE_EVALUATION_SCHEMA,
        )

        parsed = extract_anthropic_tool_input(payload, TITLE_EVALUATION_TOOL_NAME)
        if parsed is None:
            parsed = extract_json_object(extract_anthropic_text(payload))

        raw = (parsed or {}).get("titles")
        raw = raw if isinstance(raw, list) else []

        judgments: dict[str, TitleJudgment] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = string_value(item.get("title")).strip()
            if not title:
                continue
            reason = string_value(item.get("reason")).strip()
            judgments[title] = TitleJudgment(
                relevance=_clamp_score(item.get("relevance")),
                trend_reflection=_clamp_score(item.get("trendReflection")),
                purpose_match=_clamp_score(item.get("purposeMatch")),
                audience_interest=_clamp_score(item.get("audienceInterest")),
                reason=reason or None,
            )

        if not judgments:
            raise LiveAdapterError("Anthropic did not score any title")
        return judgments


def _log_empty_body(raw_final_post: Any, final_post: FinalPost, post_id: str) -> None:
    """본문이 빈 채로 파싱됐을 때 응답의 **모양**만 남긴다.

    실사례(2026-08-03): draft_llm_attempt_1이 응답 6,476바이트·출력 2,696토큰으로 성공했는데
    `body_chars=0`이 나왔다. 잘린 응답이 아니라(그건 stop_reason이 잡는다) 파서가 본문을
    꺼내지 못한 것인데, 원본 응답을 어디에도 남기지 않아 원인을 좁힐 단서가 없었다. 그 시도는
    통째로 버려지고 재생성이 돌았다 — 조용히 47초와 2,700토큰이 날아간다.

    본문은 로그에 싣지 않는다. 어떤 키가 어떤 **타입·길이**로 왔는지만 적는다.
    """
    if (final_post.body or "").strip():
        return
    if isinstance(raw_final_post, dict):
        shape = ", ".join(
            f"{key}={type(value).__name__}"
            + (f"({len(value)})" if isinstance(value, (str, list, dict)) else "")
            for key, value in sorted(raw_final_post.items())
        )
    else:
        shape = f"finalPost가 dict가 아님({type(raw_final_post).__name__})"
    logger.warning("원고 본문이 비어 파싱됨 | %s - 응답 모양: %s", short(post_id), shape)



#: 검수에 함께 올리는 이미지의 최대 장수.
#:
#: 글 한 편의 사진은 base64로 수 MB다. 전부 올리면 이 단계 하나가 원고 생성보다 비싸진다.
#: 썸네일과 본문 사진 앞쪽만 보내는 이유는, 이미지 문제(본문과 맞지 않는 그림)가 대개
#: 대표 이미지와 첫 단락들에서 드러나기 때문이다.
REVIEW_IMAGE_MAX = 4


class OpenAiFinalReviewer:
    """M4 4단계를 **한 번 더**, 다른 눈으로 본다 — 그리고 **그림을 실제로 본다.**

    Claude 쪽 검수(`AnthropicDraftGenerator.review_final_draft`)는 픽셀을 올리지 않는다.
    대체텍스트·캡션·생성 프롬프트로 '이 자리에 있을 이유가 있는가'만 판단한다. 그래서
    그림 자체가 이상한 경우(본문과 다른 장면, 뭉개진 글자)는 아무도 보지 않았다.

    여기서는 이미지를 함께 올린다(2026-08-07 사용자 결정). 두 검수는 **나란히 돌고**
    지적은 합쳐진다 — 한쪽이 놓친 것을 다른 쪽이 잡는 것이 목적이라, 순서를 두지 않는다.

    실패해도 원고를 버리지 않는다. 이 검수가 없으면 Claude 검수 결과만 쓰이고, 그 사실이
    로그에 남는다 — 호출부(`DraftService._with_final_review`)가 감싼다.
    """

    def __init__(self, role: RoleConfig):
        self._role = role
        self._api_key = _required_api_key(role)

    @staticmethod
    def _image_parts(final_post) -> list[dict]:
        """검수에 올릴 이미지. 대표 썸네일이 먼저이고, 그다음 본문 사진 순서대로."""
        ordered = []
        if final_post.featured_image is not None:
            ordered.append(final_post.featured_image)
        ordered.extend(final_post.images or [])

        parts: list[dict] = []
        seen: set[str] = set()
        for image in ordered:
            data_url = getattr(image, "data_url", "") or ""
            # data URL이 아닌 것(외부 주소·빈 값)은 보내지 않는다. 검수가 못 여는 주소를
            # 올리면 모델이 '이미지를 볼 수 없다'는 지적을 만들어 낸다.
            if not data_url.startswith("data:") or data_url in seen:
                continue
            seen.add(data_url)
            parts.append({"type": "input_image", "image_url": data_url})
            if len(parts) >= REVIEW_IMAGE_MAX:
                break
        return parts

    async def review_final_draft(self, draft_input: "DraftGenerationInput", final_post):
        images = self._image_parts(final_post)
        prompt = prompts.final_review_prompt(draft_input, final_post)
        if images:
            # 몇 번째 이미지가 무엇인지 프롬프트의 목록과 맞춰 준다. 순서를 말해 주지
            # 않으면 imageIndex가 엉뚱한 그림을 가리킨다.
            prompt += "\n\n" + prompts.final_review_image_attachment_note(len(images))

        payload = await _post_json(
            "https://api.openai.com/v1/responses",
            {"authorization": f"Bearer {self._api_key}"},
            {
                "model": self._role.model,
                "reasoning": {"effort": "low"},
                "input": [
                    {"role": "developer", "content": prompts.FINAL_REVIEW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}, *images],
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "final_review",
                        "strict": True,
                        "schema": FINAL_REVIEW_SCHEMA,
                    }
                },
            },
        )

        parsed = extract_json_object(extract_openai_text(payload))
        if not isinstance(parsed, dict):
            raise LiveAdapterError("최종 검수(OpenAI) 응답을 JSON으로 읽지 못했습니다")

        checks = final_review_checks_from_json(parsed)
        status, score = final_review_overall_from_json(parsed, checks)
        return FinalReviewReport(
            overall_status=status,
            overall_score=score,
            checks=checks,
            issues=final_review_issues_from_json(parsed),
        )

    async def critique_final_draft(
        self, draft_input: "DraftGenerationInput", final_post, model_markdown: str
    ) -> dict:
        """2차 검토 — 같은 원고를 다른 모델이 보고, **그림을 실제로 본다**(2026-08-07).

        이미지는 gpt-image-2가 만들었으므로 같은 계열이 확인한다(사용자 결정). 본문의
        [[IMAGE:n]] 자리표와 첨부 순서가 같다고 못박아, 배치 지적(imageFindings)이
        엉뚱한 그림을 가리키지 않게 한다.
        """
        images = self._image_parts(final_post)
        prompt = prompts.critique_prompt(
            draft_input, final_post, model_markdown, sees_images=bool(images)
        )
        payload = await _post_json(
            "https://api.openai.com/v1/responses",
            {"authorization": f"Bearer {self._api_key}"},
            {
                "model": self._role.model,
                "reasoning": {"effort": "low"},
                "input": [
                    {"role": "developer", "content": prompts.CRITIQUE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}, *images],
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "article_critique",
                        "strict": True,
                        "schema": CRITIQUE_SCHEMA,
                    }
                },
            },
        )
        parsed = extract_json_object(extract_openai_text(payload))
        if not isinstance(parsed, dict):
            raise LiveAdapterError("2차 원고 비평 응답을 JSON으로 읽지 못했습니다")
        return parsed

    async def verify_photo_subjects(self, article_topic: str, photos) -> list[bool]:
        """웹 검색 사진을 쓰기 전에 그림을 직접 보고 명백한 오류만 거른다.

        거절 사유는 넷뿐이다(2026-08-10 사용자 확정: "가드레일을 빡빡하게 하지 마"):
        엉뚱한 대상, 팬아트·일러스트, 사진이 아님·판독불가, 그리고 개인정보(주민등록번호·
        차량 번호판 등) 노출. 문구가 얹혔다는 이유로는 거절하지 않는다.

        검색 선정(photo_search)은 픽셀 내용을 모른다 — 검색 결과 제목·구도·해상도만
        본다. 그래서 '닷사이 23'이 제목에 든 페이지의 애니 일러스트가 만점으로 통과해
        대표 썸네일이 됐다(2026-08-07 실사례). 실제로 보이는 것의 판정은 그림을 보는
        이 모델의 일이다.

        반환은 photos와 같은 길이의 '써도 되는가' 목록. 응답에 빠진 번호는 통과로
        둔다 — 판정 누락이 멀쩡한 사진까지 버리게 하지 않는다(걸러진 자리는 호출부의
        생성 폴백이 메운다).
        """
        if not photos:
            return []
        expectations = "\n".join(
            f"{index + 1}번째 — 기대 피사체: {photo.query or article_topic}"
            + (f" (검색 결과 제목: {photo.title})" if photo.title else "")
            for index, photo in enumerate(photos)
        )
        prompt = "\n\n".join(
            [
                f"블로그 글에 실을 사진 후보들이다. 글의 소재: {article_topic}",
                "\n".join(
                    [
                        # 거절 사유는 이 넷이 전부다(2026-08-10 사용자 확정). 웹 사진에는
                        # 업로드 사진과 달리 개인정보 가리기(image_privacy)가 없으므로,
                        # 개인정보가 읽히는 사진은 여기서 거른다 — 예비 후보가 자리를 잇는다.
                        "사진마다 **실제로 보이는 것만으로** 판정한다. usable=false는 다음 넷뿐이다:",
                        # '다른 대상'의 기준(2026-08-10 사용자 확정): 막을 것은 동명이인·
                        # 이름만 같은 다른 콘텐츠다. 같은 인물이면 어느 작품·행사 사진이든
                        # 그 인물이 맞다 — 한 배우의 여러 작품 사진은 전부 쓸 수 있다.
                        "- 기대 피사체가 아니라 완전히 다른 대상이 찍혀 있다 — 동명이인,"
                        " 이름만 같은 다른 작품·프로그램처럼 글이 말하는 그 대상이 아닌"
                        " 경우다. 같은 인물·같은 작품이라면 다른 작품 활동·행사·시기의"
                        " 사진이어도 기대 피사체가 맞는 것으로 본다. 실존 인물 질의에서"
                        " '동일인인지 100% 확신할 수 없다'는 거절 사유가 아니다 — 그 이름으로"
                        " 검색된 인물 사진은 **다른 인물임이 분명할 때만** 거절한다"
                        " (프로필·보도 사진 포함).",
                        "- 손그림·팬아트 일러스트·만화 컷이다(작품이 소재라도 팬의 2차 창작 그림은 쓰지 않는다).",
                        "- 사진이 아예 없다 — 글자·도표뿐인 광고 배너·차트이거나, 무엇이 찍힌 것인지 알아볼 수 없다.",
                        "- 개인정보가 읽힌다 — 주민등록번호·신분증·여권, 차량 번호판, 전화번호·주소, 개인 계좌·서류.",
                    ]
                ),
                # 2026-08-10 사용자 결정 두 번: ① 실제 인물·작품 이미지는 가져와도 된다
                # (출처는 캡션이 표기한다), ② "카드 제외하는 가드레일을 빡빡하게 하지 마" —
                # 문구를 이유로 한 거절("합성 문구", "제3자 문구")이 진짜 개봉 포스터를
                # 두 번 연속 떨어뜨렸고(제목·개봉일 문구 → 평점 합성·배너형 판정),
                # 그 결과가 이미지 0~1장짜리 완성본이었다. 이 관문의 일은 명백한 오류
                # (엉뚱한 대상·그림·사진 아님)만 거르는 것이다.
                "문구는 거절 사유가 아니다. 사진·포스터 위에 어떤 문구가 얹혀 있어도"
                " (제목 로고·개봉일은 물론 평점·리뷰·기사 문구·자막·채널 로고까지) 그 아래가"
                " 기대 피사체의 실사진·공식 이미지면 usable=true다.",
                "영화·드라마·게임·방송 같은 작품이 소재라면, 그 작품의 공식 포스터·키아트·"
                "스틸컷·실사 장면·게임 스크린샷도 실사진과 똑같이 usable=true다.",
                "확실하지 않으면 usable=true로 둔다 — 이 관문은 명백한 오류만 거른다.",
                f"첨부 순서와 기대 피사체:\n{expectations}",
            ]
        )
        parts = [
            {"type": "input_image", "image_url": photo.data_url} for photo in photos
        ]
        payload = await _post_json(
            "https://api.openai.com/v1/responses",
            {"authorization": f"Bearer {self._api_key}"},
            {
                "model": self._role.model,
                "reasoning": {"effort": "low"},
                "input": [
                    {
                        "role": "developer",
                        "content": (
                            "You judge whether each attached image is a real photograph"
                            " of the expected subject — or, when the topic is a film,"
                            " drama, game or show, an official poster, key art, still or"
                            " screenshot of that work. Follow the Korean rules in the"
                            " user message. Return only JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}, *parts],
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "web_photo_gate",
                        "strict": True,
                        "schema": WEB_PHOTO_GATE_SCHEMA,
                    }
                },
            },
        )
        parsed = extract_json_object(extract_openai_text(payload))
        if not isinstance(parsed, dict):
            raise LiveAdapterError("웹 사진 판정 응답을 JSON으로 읽지 못했습니다")
        verdicts: dict[int, bool] = {}
        for item in parsed.get("results") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            verdicts[item["index"] - 1] = bool(item.get("usable"))
            if not item.get("usable"):
                logger.info(
                    "웹 사진 판정 탈락 %d번째: %s",
                    item["index"],
                    str(item.get("reason", ""))[:120],
                )
        return [verdicts.get(index, True) for index in range(len(photos))]
