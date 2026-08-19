"""live 어댑터가 공유하는 응답 파싱·정규화.
여기 동작은 핵심이다: 모델은 엉성한 JSON(펜스로 감싸거나 앞뒤에 산문이 붙은)과 불완전한
FinalPost를 돌려주는데, 그래도 결과를 쓸 수 있게 만드는 것이 이 헬퍼들이다.
"""

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from app.shared import (
    ARTICLE_RHYTHMS,
    ARTICLE_TYPES,
    BLOG_CATEGORIES,
    BODY_HIGHLIGHT_STYLES,
    CARD_ICON_TYPES,
    CARD_TYPES,
    CONTENT_CATEGORIES,
    CONTENT_ENTITY_TYPES,
    DECORATION_LEVELS,
    EDITORIAL_ARCHETYPES,
    EMOJI_LEVELS,
    FINAL_REVIEW_CHECK_KEYS,
    FINAL_REVIEW_CHECK_STATUSES,
    FINAL_REVIEW_ISSUE_KINDS,
    FINAL_REVIEW_OVERALL_STATUSES,
    INFOGRAPHIC_VARIANTS,
    NAMED_SUBJECT_KINDS,
    PHOTO_FRAMINGS,
    PHOTO_ROLES,
    IMAGE_SOURCES,
    PHOTO_SOURCE_MODES,
    POLISH_EDIT_KINDS,
    PROCESS_VARIANTS,
    REAL_IMAGE_TYPES,
    REFERENCE_IMAGE_ROLES,
    SECTION_PURPOSES,
    SOURCE_TYPES,
    TABLE_VARIANTS,
    THUMBNAIL_COPY_MODES,
    THUMBNAIL_LAYOUTS,
    TITLE_STRATEGIES,
    VISUAL_DENSITY_LEVELS,
    VISUAL_SUBJECT_KINDS,
    VISUAL_THEMES,
    VISUAL_TYPES,
    VOICE_MODES,
    CardBrief,
    CardDesignSystem,
    CardScene,
    ContentEntityProfile,
    ContentPlan,
    ContentPlanSection,
    EditorialStylePlan,
    FinalPost,
    FinalReviewCheck,
    FinalReviewIssue,
    PlannedVisual,
    PolishEdit,
    PrivateRegion,
    ReferenceEvidenceProfile,
    ReferenceImageEvidence,
    RelatedPerson,
    SearchSource,
    SeoKeywordPlan,
    SourceDataPoint,
    TitlePlan,
    VisualBudget,
    VisualCardPlan,
    VisualDataPoint,
    VisualGroup,
    VisualStep,
    VisualTableRow,
    WritingDirection,
)
from app.shared.format import escape_html

from .imaging import thumbnail_lines
from .keyword_naturalization import is_entity_juxtaposition
from .markdown_html import markdown_for_storage, markdown_to_html


class LiveAdapterError(Exception):
    pass


class ProviderOverloadedError(LiveAdapterError):
    """제공자가 일시적으로 혼잡해 재시도를 다 쓰고도 실패했다(5xx·429).

    잘못된 요청·키·모델과 **구분해야 하는** 실패다. 사용자가 할 수 있는 일이 다르다:
    혼잡은 잠시 뒤 다시 누르면 대개 풀리고, 설정 오류는 눌러도 영원히 같은 화면이다.
    화면에 무엇을 안내할지 정하려면 이 구분이 예외 타입에 남아 있어야 한다 —
    문자열을 뒤져 판단하면 제공자가 메시지를 바꿀 때 조용히 깨진다.
    """

    def __init__(self, provider: str, model: str, status: int, detail: str):
        self.provider = provider
        self.model = model
        self.status = status
        self.detail = detail
        super().__init__(f"provider request failed with {status}: {detail}")


class ProviderEmptyResponseError(LiveAdapterError):
    """200으로 왔는데 **최종 본문이 없다**(2026-08-12 사용자 신고).

    검색 단계(steps)는 다 남아 있고 응답도 15만 바이트가 넘는데, 모델이 마지막 답을 쓰지
    않고 끝낸 경우다. 잘못된 키·모델·요청과 **구분해야 한다** — 그쪽은 다시 눌러도 영원히
    같은 화면이지만, 이쪽은 같은 일을 다른 모델로 다시 시켜 보면 대개 된다.

    타입이 없던 동안 이 실패는 평범한 ``LiveAdapterError``였고, 폴백 모델을 하나도 시도해
    보지 못한 채 검증이 통째로 실패했다 — 화면에는 방향 후보 4개 대신 '자료를 모으는 중
    오류가 났습니다' 한 장만 남았다.
    """

    def __init__(self, message: str):
        super().__init__(message)


class ProviderStopReasonError(LiveAdapterError):
    """HTTP 200으로 왔지만 응답이 쓸 수 없는 상태다(`stop_reason`이 알려 준다).

    이 계열이 필요한 이유: Opus 5는 거절·컨텍스트 초과·출력 잘림을 **오류 응답이 아니라
    200 + stop_reason**으로 알려 준다. 예전 코드는 stop_reason을 읽지 않아서, 잘린
    `tool_use.input`이 부분 dict로 파서에 넘어가고 `final_post_from_json`이 예외 없이
    폴백했다 — 실패가 오류가 아니라 조용한 품질 저하로 나갔다. 타입으로 구분해야
    화면 안내와 재시도 정책을 다르게 줄 수 있다.
    """

    def __init__(self, message: str, *, stage: str, model: str, stop_reason: str):
        self.stage = stage
        self.model = model
        self.stop_reason = stop_reason
        super().__init__(message)


class ProviderRefusedError(ProviderStopReasonError):
    """모델 안전 정책이 요청을 거절했다(`stop_reason="refusal"`)."""

    def __init__(self, *, stage: str, model: str, category: str | None):
        self.category = category
        super().__init__(
            f"provider refused the request at {stage} (category={category or 'unknown'})",
            stage=stage,
            model=model,
            stop_reason="refusal",
        )


class ProviderContextExceededError(ProviderStopReasonError):
    """입력과 응답이 모델 컨텍스트 한도를 넘었다(`model_context_window_exceeded`)."""

    def __init__(self, *, stage: str, model: str):
        super().__init__(
            f"provider hit the context window at {stage}",
            stage=stage,
            model=model,
            stop_reason="model_context_window_exceeded",
        )


class ProviderTruncatedError(ProviderStopReasonError):
    """최대 출력 토큰에 도달해 결과가 잘렸다(`stop_reason="max_tokens"`).

    잘린 JSON을 파싱 오류로 흘려보내지 않는다 — 잘림은 파싱 문제가 아니라 예산 문제이고,
    고치는 방법(max_tokens 조정)도 다르다.
    """

    def __init__(self, *, stage: str, model: str, max_tokens: int):
        self.max_tokens = max_tokens
        super().__init__(
            f"provider output was truncated at {stage} (max_tokens={max_tokens})",
            stage=stage,
            model=model,
            stop_reason="max_tokens",
        )


_FENCED = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$", re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    """모델 응답에서 JSON 객체를 파싱한다. ``` 펜스와 앞뒤 산문을 허용하고(안 되면 첫 {와
    마지막 } 사이를 잘라 낸다)."""
    trimmed = text.strip()
    fenced = _FENCED.match(trimmed)
    source = fenced.group(1) if fenced else trimmed

    try:
        parsed = json.loads(source)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        first = source.find("{")
        last = source.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(source[first : last + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    raise LiveAdapterError("provider did not return a JSON object")


def string_value(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def string_array(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


# 사용자에게 그대로 보이는 문장(의도 rationale·자료 summary)에 모델이 이따금 내부 처리 얘기를
# 흘린다 — "Snippet이 비어 있어…", "sources에서 제외했다" 같은 문장. 프롬프트로 막지만 100%는
# 아니라, 이런 내부 용어가 든 문장은 마지막에 걷어낸다. 아래 표시는 한국어 블로그 의도·자료
# 설명에 정상적으로는 나오지 않는 내부 용어라 오탐 위험이 낮다.
_INTERNAL_NOTE_MARKERS = ("snippet", "스니펫", "sourceindex", "source index", "sources에서")


def strip_internal_notes(text: str) -> str:
    """내부 처리 용어가 든 문장을 사용자 문구에서 제거한다. 남는 게 없으면 원문을 그대로
    돌려주지 않고 빈 문자열을 준다 — 내부 얘기를 보여 주느니 비우는 편이 낫다."""
    if not text or not any(marker in text.lower() for marker in _INTERNAL_NOTE_MARKERS):
        return text
    # 문장 단위(한국어 '~다.'와 . ! ? 。, 개행)로 끊어 내부 용어가 든 문장만 버린다.
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text)
    kept = [
        part.strip()
        for part in parts
        if part.strip() and not any(marker in part.lower() for marker in _INTERNAL_NOTE_MARKERS)
    ]
    return " ".join(kept)


def _source_type_value(value: Any) -> str:
    """분류가 목록에 없으면 신뢰하지 않고 빈 값으로 둔다 — 화면은 '기타'로 다룬다."""
    text = string_value(value).strip().upper()
    return text if text in SOURCE_TYPES else ""


def _relevance_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(100, int(value)))


def _data_points_value(value: Any) -> list[SourceDataPoint] | None:
    """자료의 실측 수치. 숫자가 아닌 값·형식 오류는 버린다 — 통계는 확실한 것만."""
    if not isinstance(value, list):
        return None
    points: list[SourceDataPoint] = []
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        label = string_value(item.get("label")).strip()
        raw = item.get("value")
        if not label or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        unit = string_value(item.get("unit")).strip() or None
        points.append(SourceDataPoint(label=label, value=float(raw), unit=unit))
    return points or None


def _verified_data_points(value: Any, source: SearchSource) -> list[SourceDataPoint] | None:
    """요약 모델이 뽑은 수치가 실제 검색 인용문에 적혀 있는 항목만 남긴다."""
    if source.data_points:
        return source.data_points
    corpus = f"{source.title} {source.snippet}".lower()
    verified: list[SourceDataPoint] = []
    for point in _data_points_value(value) or []:
        numeric = f"{point.value:g}"
        number_present = re.search(
            rf"(?<![\d.]){re.escape(numeric)}(?:\.0+)?(?![\d.])", corpus
        )
        unit_present = not point.unit or point.unit.lower() in corpus
        if point.label.lower() in corpus and number_present and unit_present:
            verified.append(point)
    return verified or None


def _normalized_http_url(value: Any) -> str | None:
    raw = string_value(value).strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def sources_value(
    value: Any, allowed_sources: list[SearchSource] | None = None
) -> list[SearchSource]:
    if not isinstance(value, list):
        return []
    allowed_by_url = {
        normalized: source
        for source in (allowed_sources or [])
        if (normalized := _normalized_http_url(source.url)) is not None
    }
    result = []
    for item in value[:5]:
        source = item if isinstance(item, dict) else {}
        normalized_url = _normalized_http_url(source.get("url"))
        if normalized_url is None:
            continue

        authoritative = allowed_by_url.get(normalized_url) if allowed_sources is not None else None
        if allowed_sources is not None and authoritative is None:
            continue

        result.append(
            SearchSource(
                title=(
                    authoritative.title
                    if authoritative
                    else string_value(source.get("title")).strip() or normalized_url
                ),
                url=authoritative.url if authoritative else normalized_url,
                snippet=(
                    authoritative.snippet
                    if authoritative
                    else string_value(source.get("snippet"))
                ),
                source_type=(
                    authoritative.source_type
                    if authoritative
                    else _source_type_value(source.get("sourceType"))
                ),
                relevance_score=_relevance_value(source.get("relevanceScore")),
                # 요약 모델이 출처에 없던 숫자를 추가하지 못하게 한다.
                data_points=(
                    _verified_data_points(source.get("dataPoints"), authoritative)
                    if authoritative
                    else _data_points_value(source.get("dataPoints"))
                ),
            )
        )
    return result


def sources_from_indexes(value: Any, collected: list[SearchSource]) -> list[SearchSource]:
    """모델이 sourceIndex(수집 목록 번호, 1부터)로 가리킨 출처를 실제 수집 자료와 합친다.

    제목·URL·스니펫은 수집본이 진실이다 — 모델에게 그 문자열을 다시 베끼게 하는 것은
    출력 토큰 낭비였고, 베끼다 바꾸면(URL 오타 등) 없는 출처가 된다. 판단이 필요한
    값(sourceType·relevanceScore·dataPoints)만 모델의 것을 쓴다. 목록 밖 번호와 중복
    번호는 버린다 — 지어낸 참조를 유효한 출처처럼 취급하지 않는다."""
    if not isinstance(value, list):
        return []
    result: list[SearchSource] = []
    seen: set[int] = set()
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        index = item.get("sourceIndex")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if not (1 <= index <= len(collected)) or index in seen:
            continue
        seen.add(index)
        base = collected[index - 1]
        # 모델이 쓴 한 줄 요약(summary)을 스니펫으로 쓴다 — 수집 grounding의 cited_text가
        # 비어 있어도 검증 화면에 자료 설명이 보이게 한다. 요약이 비면 수집본 스니펫을 유지.
        # 내부 처리 얘기(Snippet 비었다 등)가 섞여 오면 걷어낸다.
        summary = strip_internal_notes(string_value(item.get("summary")).strip())
        result.append(
            base.model_copy(
                update={
                    "snippet": summary or base.snippet,
                    "source_type": _source_type_value(item.get("sourceType"))
                    or base.source_type,
                    "relevance_score": _relevance_value(item.get("relevanceScore")),
                    "data_points": _data_points_value(item.get("dataPoints"))
                    or base.data_points,
                }
            )
        )
    return result


def _seo_norm(text: str) -> str:
    """SEO 키워드 비교용 정규화. 조사·띄어쓰기·대소문자 차이만 다른 키워드를 같은 것으로
    본다(quality.normalize_for_match와 같은 규칙이지만, llm 계층이 modules를 import하지
    않도록 여기 따로 둔다)."""
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())


# 확정 제목에서 핵심 검색 구문을 뽑을 때 후보로 삼는 최대 어절 수. 네 어절을 넘어가면
# 검색어가 아니라 문장이라, SEO 키워드로서 쓸모가 없다.
_MAX_KEYWORD_WORDS = 3
# 제목을 어절로 자를 때 떼어내는 구두점·따옴표. 'BTS,'와 'BTS'는 같은 말이다.
_WORD_TRIM = " \t\"'“”‘’.,!?()[]{}<>:;·…~/|\\-–—"


def _title_words(title: str) -> list[str]:
    """제목을 어절 단위로 자른다. 붙어 있는 구두점은 떼고 알맹이만 남긴다."""
    words = [raw.strip(_WORD_TRIM) for raw in (title or "").split()]
    return [word for word in words if word]


def keyword_inside_title(
    title: str, preferred: str, fallbacks: tuple[str, ...] = ()
) -> str:
    """제목 안에 **실제로 들어 있는** 핵심 검색 구문을 고른다.

    사용자가 M2에서 고른 제목은 바꿀 수 없는 값이다. 그런데 모델이 그 제목에 없는 구문을
    핵심 키워드로 내놓으면(제목 '월드투어 다시 시작하는 BTS…'에 키워드 'BTS 월드투어')
    어느 단계도 그것을 고칠 수 없다 — 원고를 몇 번 다시 써도 제목은 그대로이므로 생성 후
    검증(seo_primary_in_title)이 매번 같은 이유로 실패한다. 제목이 확정값이니 양보할 쪽은
    키워드다. 그래서 제목을 반려하는 대신 제목 안에서 키워드를 고른다.

    선호 구문이 제목 안에 있으면 그대로 쓴다. 없으면 선호 구문(과 fallbacks)의 단어를
    가장 많이 담은 제목 어절 묶음을 고르고, 그마저 없으면 제목에서 가장 긴 어절을 쓴다.
    제목이 비어 있으면 고를 근거가 없으므로 선호 구문을 그대로 돌려준다.
    """
    title_norm = _seo_norm(title)
    preferred = (preferred or "").strip()
    if not title_norm:
        return preferred
    if preferred and _seo_norm(preferred) in title_norm:
        return preferred

    words = _title_words(title)
    if not words:
        return preferred

    wanted = {
        _seo_norm(token)
        for source in (preferred, *fallbacks)
        for token in re.findall(r"[0-9A-Za-z가-힣]+", (source or "").lower())
    }
    wanted.discard("")

    best = ""
    best_score = (-1, -1, -1)
    for size in range(1, _MAX_KEYWORD_WORDS + 1):
        for start in range(len(words) - size + 1):
            chunk = words[start : start + size]
            phrase = " ".join(chunk)
            phrase_norm = _seo_norm(phrase)
            # 어절 사이에 구두점이 있었으면('BTS, 일정') 이어 붙인 구문이 제목에 없다.
            if not phrase_norm or phrase_norm not in title_norm:
                continue
            hits = sum(
                1
                for word in chunk
                if any(token in _seo_norm(word) for token in wanted)
            )
            # 모든 어절이 선호 구문에서 온 묶음을 먼저 고른다(온전한 일치 > 부분 일치).
            # 그다음 더 긴 구문, 마지막으로 더 구체적인(글자 수가 많은) 것을 고른다.
            score = (hits == size, hits, len(phrase_norm))
            if score > best_score:
                best, best_score = phrase, score

    return best or max(words, key=len)


def title_plan_from_json(value: Any, fixed_title: str | None = None) -> TitlePlan | None:
    """제목 계획 파싱. 형식이 깨졌으면 None — 호출부가 제목 계획 없이 예전 동작으로 쓴다.

    h1은 모델이 무엇을 주든 primary_title로 세운다. 규격상 둘은 같아야 하는데 모델이 어길
    수 있고, 어겼을 때 되물어 볼 값이 아니라 이미 아는 값이기 때문이다.
    fixed_title(사용자가 M2에서 고른 트렌드 제목)이 있으면 그 제목이 이긴다 — 사용자가 이미
    고른 것을 모델이 다듬어 놓는 일을 코드가 막는다.

    제목이 고정된 글에서는 primary_keyword도 그 제목 안에서 고른다. 제목을 바꿀 수 없는데
    제목에 없는 키워드를 그대로 두면 check_title_plan이 계획을 반려하고, 두 번 반려되면
    확정 제목 자체가 사라진다 — 고칠 수 있는 것(키워드) 때문에 고칠 수 없는 것(제목)을
    버리는 셈이다. 제목을 사용자가 정하지 않은 글은 손대지 않는다: 그쪽은 모델이 제목을
    다시 써서 키워드를 넣을 수 있으므로, 규격 위반을 그대로 알리는 것이 맞다.
    """
    plan = value.get("titlePlan") if isinstance(value, dict) else None
    if not isinstance(plan, dict):
        return None

    fixed = (fixed_title or "").strip()
    primary_title = fixed or string_value(plan.get("primaryTitle")).strip()
    if not primary_title:
        return None

    primary_keyword = string_value(plan.get("primaryKeyword")).strip()
    if fixed:
        primary_keyword = keyword_inside_title(primary_title, primary_keyword)

    strategy = string_value(plan.get("titleStrategy")).strip().upper()
    alternatives = [
        title
        for title in (t.strip() for t in string_array(plan.get("alternativeTitles"))[:4])
        if title and title != primary_title
    ]
    return TitlePlan(
        primary_title=primary_title,
        alternative_titles=alternatives,
        h1=primary_title,
        primary_keyword=primary_keyword,
        title_strategy=strategy if strategy in TITLE_STRATEGIES else "SEARCH_INTENT",
    )


def seo_keyword_plan_from_json(
    value: Any,
    title_plan: TitlePlan | None = None,
    fixed_title: str | None = None,
) -> SeoKeywordPlan | None:
    """SEO 키워드 계획 파싱 + 정규화(validateSeoKeywordPlan). 형식이 깨졌으면 None —
    호출부가 계획 없이 예전 동작으로 진행한다(SEO 계획은 품질 장치다).

    정규화가 곧 검증이다: 빈 문자열 제거, primary/secondary 중복 제거(조사·띄어쓰기 차이
    포함), avoid에서 primary·secondary와 겹치는 것 제거. 그리고 primary는 확정 제목 안에
    실제로 들어 있어야 생성 후 검증(seo_primary_in_title)이 통과하므로, 제목을 아는 경우
    모델 primary가 제목에 있을 때만 그대로 쓰고 아니면 제목에서 고른 구문으로 고정한다
    (align_seo_plan_with_title).

    제목은 title_plan에서 오는 것이 보통이지만, 제목 계획을 만들지 못한 글에도 사용자가
    M2에서 고른 제목(fixed_title)은 있다. 그때도 고정해야 한다 — 제목 계획이 없다고 검증만
    실패하는 원고가 남으면 안 된다."""
    plan = value.get("seoKeywordPlan") if isinstance(value, dict) else None
    if not isinstance(plan, dict):
        return None

    requested = string_value(plan.get("primary")).strip()
    anchor_title = (
        title_plan.primary_title if title_plan is not None else ""
    ).strip() or (fixed_title or "").strip()
    primary = requested
    if anchor_title:
        primary = keyword_inside_title(
            anchor_title,
            requested,
            fallbacks=(
                ((title_plan.primary_keyword or ""),) if title_plan is not None else ()
            ),
        )
    if not primary:
        return None

    primary_norm = _seo_norm(primary)
    secondary: list[str] = []
    seen = {primary_norm}
    # 밀려난 primary도 모델이 이유가 있어 고른 검색어다. 버리지 않고 secondary 맨 앞에
    # 두면, 제목에는 못 들어가도 본문에서는 그대로 쓰인다.
    for item in [requested, *string_array(plan.get("secondary"))]:
        text = item.strip()
        norm = _seo_norm(text)
        if not text or not norm or norm in seen:
            continue
        seen.add(norm)
        secondary.append(text)

    avoid: list[str] = []
    avoid_seen: set[str] = set()
    # avoid는 반드시 쓰는 키워드(primary·secondary)와 같은 표현을 담지 않는다.
    used = {primary_norm, *(_seo_norm(s) for s in secondary)}
    for item in string_array(plan.get("avoid")):
        text = item.strip()
        norm = _seo_norm(text)
        if not text or not norm or norm in used or norm in avoid_seen:
            continue
        avoid_seen.add(norm)
        avoid.append(text)

    return SeoKeywordPlan(primary=primary, secondary=secondary[:8], avoid=avoid[:8])


def align_seo_plan_with_title(
    plan: SeoKeywordPlan | None, title: str | None, preferred_keyword: str | None = None
) -> SeoKeywordPlan | None:
    """이미 만들어진 SEO 계획을 이 글의 확정 제목에 다시 맞춘다.

    계획은 한 번 만들어 DB에 저장하고 재사용한다(제목 계획과 같은 정책). 그래서 제목이
    확정되기 전에 만들어진 계획 — 예를 들어 제목 계획 생성이 실패한 선행 생성이 저장한 것 —
    이 그대로 돌아올 수 있고, 그 primary가 확정 제목에 없으면 생성 후 검증이 매번 같은
    이유로 실패한다. 파싱 시점의 고정만으로는 이 경로를 못 막으므로, **쓰는 시점에** 한 번 더
    맞춘다. 이미 제목 안에 있는 primary는 그대로 둔다(반환값도 같은 객체다).
    """
    anchor = (title or "").strip()
    if plan is None or not anchor:
        return plan
    primary = keyword_inside_title(
        anchor, plan.primary, fallbacks=((preferred_keyword or ""),)
    )
    if primary == plan.primary:
        return plan

    seen = {_seo_norm(primary)}
    secondary: list[str] = []
    for text in [plan.primary, *plan.secondary]:
        norm = _seo_norm(text)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        secondary.append(text)
    return plan.model_copy(update={"primary": primary, "secondary": secondary[:8]})


def _length_share_value(value: Any) -> str:
    """섹션 분량 비중. '25~35%'처럼 숫자와 %가 함께 있을 때만 남긴다.

    이 값은 원고 프롬프트에 그대로 실리고 분량 수정 지시가 참조한다. '적당히'·'길게' 같은
    말이 섞여 들어오면 어디를 얼마나 늘릴지 계산할 수 없으므로, 형식이 아니면 버린다.
    """
    text = string_value(value).strip()
    if not text or len(text) > 20 or "%" not in text:
        return ""
    return text if any(char.isdigit() for char in text) else ""


def content_plan_from_json(value: Any) -> ContentPlan | None:
    """콘텐츠 설계 파싱. 설계는 품질 장치라 형식이 깨졌으면 None으로 두고 설계 없이 쓴다 —
    원고 생성을 막을 이유가 아니다. 섹션 id는 순번으로 다시 매겨 일관되게 만든다."""
    plan = value.get("contentPlan") if isinstance(value, dict) else None
    if not isinstance(plan, dict):
        return None
    raw_sections = plan.get("sections")
    if not isinstance(raw_sections, list):
        return None

    sections: list[ContentPlanSection] = []
    for item in raw_sections[:6]:
        if not isinstance(item, dict):
            continue
        heading = string_value(item.get("heading")).strip()
        if not heading:
            continue
        purpose = string_value(item.get("purpose")).strip()
        visual = string_value(item.get("visualType")).strip().upper()
        sections.append(
            ContentPlanSection(
                section_id=f"section-{len(sections) + 1}",
                heading=heading,
                question=string_value(item.get("question")).strip(),
                purpose=purpose if purpose in SECTION_PURPOSES else "근거 제시",
                key_points=string_array(item.get("keyPoints"))[:5],
                evidence_ids=string_array(item.get("evidenceIds"))[:5],
                visual_type=visual if visual in VISUAL_TYPES else "NONE",
                visual_reason=string_value(item.get("visualReason")).strip() or None,
                # 아래 여섯은 없어도 설계를 버리지 않는다. 빈 값이면 원고 프롬프트가 그 줄을
                # 빼고 나머지 설계를 그대로 쓴다 — 한 칸이 비었다고 설계 전체를 잃을 이유가 없다.
                interpretation=string_value(item.get("interpretation")).strip(),
                omit_background=string_value(item.get("omitBackground")).strip(),
                connection=string_value(item.get("connection")).strip(),
                length_share=_length_share_value(item.get("lengthShare")),
                persona_detail=string_value(item.get("personaDetail")).strip(),
                forbidden_claims=string_array(item.get("forbiddenClaims"))[:4],
                target_reader_need=string_value(item.get("targetReaderNeed")).strip(),
                tone_direction=string_value(item.get("toneDirection")).strip(),
            )
        )
    if len(sections) < 3:
        return None

    article_type = string_value(plan.get("articleType")).strip().upper()
    return ContentPlan(
        target_reader=string_value(plan.get("targetReader")).strip(),
        reader_problem=string_value(plan.get("readerProblem")).strip(),
        reader_question=string_value(plan.get("readerQuestion")).strip(),
        article_promise=string_value(plan.get("articlePromise")).strip(),
        content_angle=string_value(plan.get("contentAngle")).strip(),
        article_type=article_type if article_type in ARTICLE_TYPES else "INFORMATION",
        tone=string_value(plan.get("tone")).strip() or None,
        sections=sections,
    )


def _score_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(100.0, float(value)))


def _confidence_value(value: Any) -> float:
    """0~1 신뢰도. 값이 없거나 이상하면 0(모름) — 없는 확신을 만들어 내지 않는다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _enum_value(value: Any, allowed: tuple[str, ...], fallback: str = "") -> str:
    """목록 밖의 값은 믿지 않고 폴백으로 되돌린다 — 모르는 값이 그대로 렌더러까지 가면
    조용히 기본 스타일로 그려지고, 왜 그런지는 아무 데도 남지 않는다."""
    text = string_value(value).strip().upper()
    return text if text in allowed else fallback


def _int_value(value: Any, fallback: int, *, low: int = 0, high: int = 10) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(low, min(high, int(value)))


def final_review_checks_from_json(value: Any) -> dict[str, FinalReviewCheck]:
    """항목별 판정. 빠진 항목은 'skipped'로 채운다.

    빠진 것을 통과로 채우지 않는 이유: 검사하지 못한 것과 통과한 것은 다르다. 통과로 채우면
    모델이 한 항목을 통째로 잊어도 결과에는 '문제 없음'으로 남는다.
    """
    raw_checks = value.get("checks") if isinstance(value, dict) else None
    raw_checks = raw_checks if isinstance(raw_checks, dict) else {}

    checks: dict[str, FinalReviewCheck] = {}
    for key in FINAL_REVIEW_CHECK_KEYS:
        item = raw_checks.get(key)
        if not isinstance(item, dict):
            checks[key] = FinalReviewCheck(status="skipped", reason="검수가 이 항목을 반환하지 않았습니다")
            continue
        status = string_value(item.get("status")).strip().lower()
        if status not in FINAL_REVIEW_CHECK_STATUSES:
            status = "skipped"
        checks[key] = FinalReviewCheck(
            status=status,
            reason=string_value(item.get("reason")).strip(),
            affected_sections=[
                text for text in string_array(item.get("affectedSections"))[:8] if text.strip()
            ],
        )
    return checks


def final_review_overall_from_json(value: Any, checks: dict[str, FinalReviewCheck]) -> tuple[str, int]:
    """(전체 판정, 점수). 모델 값이 항목별 판정과 어긋나면 **항목별 판정을 믿는다**.

    모델이 fail을 적어 놓고 overallStatus를 pass로 두는 일이 있다. 그때 모델 말을 그대로
    쓰면 고쳐야 할 글이 통과로 저장된다 — 판정의 근거는 항목별 결과이므로 그쪽을 기준으로
    되돌린다. 점수도 같은 이유로 상한을 건다.
    """
    statuses = [check.status for check in checks.values()]
    has_fail = "fail" in statuses
    has_warning = "warning" in statuses

    raw = value.get("overallStatus") if isinstance(value, dict) else None
    status = string_value(raw).strip().lower()
    if status not in FINAL_REVIEW_OVERALL_STATUSES:
        status = "revise" if has_fail else "warning" if has_warning else "pass"
    if has_fail:
        status = "revise"
    elif has_warning and status == "pass":
        status = "warning"

    raw_score = value.get("overallScore") if isinstance(value, dict) else None
    score = (
        int(raw_score)
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
        else (60 if has_fail else 80 if has_warning else 100)
    )
    score = max(0, min(100, score))
    # fail이 있는데 90점처럼 적어 오는 것을 막는다. 상한은 판정에서 나온다.
    if has_fail:
        score = min(score, 79)
    elif has_warning:
        score = min(score, 94)
    return status, score


def final_review_issues_from_json(value: Any) -> list[FinalReviewIssue]:
    """최종 검수 응답 → 지적 목록. 형식이 깨진 항목은 통째로 버린다.

    검수는 원고 생성의 관문이 아니라 그 위에 얹은 마무리다. 응답이 이상하면 '지적 없음'으로
    두는 편이 맞다 — 여기서 예외를 던지면 이미 완성된 원고가 통째로 실패한다.
    """
    if not isinstance(value, dict):
        return []
    raw_issues = value.get("issues")
    if not isinstance(raw_issues, list):
        return []

    issues: list[FinalReviewIssue] = []
    for item in raw_issues[:12]:
        if not isinstance(item, dict):
            continue
        kind = string_value(item.get("kind")).strip().lower()
        if kind not in FINAL_REVIEW_ISSUE_KINDS:
            continue
        severity = string_value(item.get("severity")).strip().lower()
        if severity not in ("critical", "minor"):
            severity = "minor"
        raw_index = item.get("imageIndex")
        image_index = (
            int(raw_index)
            if isinstance(raw_index, (int, float)) and not isinstance(raw_index, bool)
            else None
        )
        issue = FinalReviewIssue(
            kind=kind,
            severity=severity,
            reason=string_value(item.get("reason")).strip(),
            quote=string_value(item.get("quote")),
            replacement=string_value(item.get("replacement")),
            image_index=image_index,
        )
        # 고칠 자리를 못 찾는 지적은 남겨도 아무 일도 못 한다. 본문 지적은 quote가,
        # 이미지 지적은 번호가 있어야 실제로 적용된다.
        if issue.kind == "image":
            if issue.image_index is None:
                continue
        elif not issue.quote.strip():
            continue
        issues.append(issue)
    return issues


def polish_edits_from_json(value: Any) -> list[PolishEdit]:
    """문장 다듬기 응답 → 교정 목록. 형식이 깨진 항목은 통째로 버린다.

    검수와 같은 이유로 예외를 던지지 않는다 — 다듬기는 완성된 원고 위에 얹은 마무리이고,
    응답이 이상하면 '고칠 것 없음'으로 두는 편이 맞다.

    여기서 걸러 내는 것은 **형식**뿐이다(종류를 모르겠거나, 고칠 자리가 없거나, 바뀐 것이
    없는 항목). 내용이 규칙을 어겼는지 — 새 숫자를 넣었는지, 지어낸 경험인지 — 는
    modules/draft/polish.py가 원고와 대조해 판단한다.
    """
    if not isinstance(value, dict):
        return []
    raw_edits = value.get("edits")
    if not isinstance(raw_edits, list):
        return []

    edits: list[PolishEdit] = []
    for item in raw_edits[:12]:
        if not isinstance(item, dict):
            continue
        kind = string_value(item.get("kind")).strip().lower()
        if kind not in POLISH_EDIT_KINDS:
            continue
        before = string_value(item.get("before"))
        after = string_value(item.get("after"))
        # 고칠 자리가 없거나(before 없음) 고친 것이 없는(before == after) 항목은 남겨도
        # 아무 일도 하지 않는다.
        if not before.strip() or before == after:
            continue
        edits.append(
            PolishEdit(
                kind=kind,
                reason=string_value(item.get("reason")).strip(),
                before=before,
                after=after,
            )
        )
    return edits


def content_entity_from_json(value: Any, raw_keyword: str = "") -> ContentEntityProfile | None:
    """소재가 실제로 무엇인지(유형·정식명·핵심 포맷). 형식이 깨졌으면 None.

    ``raw_keyword``는 모델이 아니라 코드가 채운다 — 사용자가 무엇을 골랐는지는 이미 아는
    값이고, 여기를 모델에 맡기면 '글에 쓸 표현'과 다시 뒤섞인다. 금지 표현에도 그 원본
    검색어를 코드가 넣어 둔다: 이번 문제의 출발점이 정확히 그 문자열이기 때문이다.
    """
    entity = value.get("contentEntity") if isinstance(value, dict) else None
    if not isinstance(entity, dict):
        return None

    people: list[RelatedPerson] = []
    for item in entity.get("relatedPeople") or []:
        if not isinstance(item, dict):
            continue
        name = string_value(item.get("name")).strip()
        if not name:
            continue
        people.append(
            RelatedPerson(name=name, relation=string_value(item.get("relation")).strip())
        )

    canonical_name = string_value(entity.get("canonicalName")).strip()
    forbidden = string_array(entity.get("forbiddenPhrases"))[:6]
    keyword = (raw_keyword or "").strip()
    # 검색어 조합 자체를 금지 표현에 넣는다. 단, **서로 다른 고유명사를 나란히 둔 조합**일
    # 때만이다 — '전과자 학과 체험'처럼 그 자체로 자연스러운 명사구까지 금지하면, 멀쩡한
    # 문장을 쓸 수 없게 된다.
    names = [canonical_name, *(p.name for p in people)]
    if (
        keyword
        and is_entity_juxtaposition(keyword, names)
        and keyword not in forbidden
    ):
        forbidden = [keyword, *forbidden][:6]

    # 보조 카테고리가 메인과 같으면 버린다 — 같은 지침을 두 번 싣는 것은 강조가 아니라
    # 중복이고, 프롬프트 블록이 같은 문장을 반복하게 된다.
    primary_category = _enum_value(entity.get("primaryCategory"), BLOG_CATEGORIES, "")
    secondary_category = _enum_value(entity.get("secondaryCategory"), BLOG_CATEGORIES, "")
    if secondary_category == primary_category:
        secondary_category = ""

    return ContentEntityProfile(
        entity_type=_enum_value(
            entity.get("entityType"), CONTENT_ENTITY_TYPES, "GENERAL_TOPIC"
        ),
        primary_category=primary_category,
        secondary_category=secondary_category,
        writing_mode=string_value(entity.get("writingMode")).strip(),
        canonical_name=canonical_name,
        brand=string_value(entity.get("brand")).strip(),
        raw_keyword=keyword,
        platform=string_value(entity.get("platform")).strip(),
        official_channel=string_value(entity.get("officialChannel")).strip(),
        related_people=people[:6],
        core_format=string_value(entity.get("coreFormat")).strip(),
        primary_activities=string_array(entity.get("primaryActivities"))[:6],
        secondary_activities=string_array(entity.get("secondaryActivities"))[:6],
        background_scenes=string_array(entity.get("backgroundScenes"))[:6],
        official_video_queries=string_array(entity.get("officialVideoQueries"))[:4],
        natural_phrases=string_array(entity.get("naturalPhrases"))[:6],
        forbidden_phrases=forbidden,
        requires_fresh_research=bool(entity.get("requiresFreshResearch")),
        requires_real_images=bool(entity.get("requiresRealImages")),
        real_image_type=_enum_value(
            entity.get("realImageType"), REAL_IMAGE_TYPES, "NONE"
        ),
        confidence=_confidence_value(entity.get("confidence")),
    )


def _private_regions(value: Any) -> list[PrivateRegion]:
    """개인정보 자리 좌표를 읽는다. **읽히지 않는 상자는 조용히 버린다.**

    좌표가 이상하다고 원고 생성을 실패시키지 않는다. 다만 버린 상자는 덮이지 않으므로,
    모델이 0~1 비율로 답하도록 스키마 설명에 못 박아 두었다(`llm/schemas.py`).
    """
    regions: list[PrivateRegion] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        try:
            regions.append(
                PrivateRegion(
                    x=float(item.get("x")),
                    y=float(item.get("y")),
                    width=float(item.get("width")),
                    height=float(item.get("height")),
                    kind=string_value(item.get("kind")).strip(),
                )
            )
        except (TypeError, ValueError, ValidationError):
            continue
    return regions[:8]


def reference_evidence_profile_from_json(
    value: Any, raw_keyword: str = ""
) -> ReferenceEvidenceProfile | None:
    """모델이 읽어 낸 참고자료 근거. 형식이 깨졌으면 None — 코드가 만든 뼈대만 쓴다.

    has_references·has_user_experience_evidence는 여기서 읽지 않는다. 그 둘은 첨부와 메모
    문자열로 코드가 판정하는 값이고, 모델에 맡기면 "사진이 있으니 경험도 있다"는 낙관이
    그대로 통과한다(modules/draft/reference_evidence.enrich).
    """
    profile = value.get("referenceEvidenceProfile") if isinstance(value, dict) else None
    if not isinstance(profile, dict):
        return None

    roles: list[ReferenceImageEvidence] = []
    for item in profile.get("referenceImageRoles") or []:
        if not isinstance(item, dict):
            continue
        reference_id = string_value(item.get("referenceId")).strip()
        if not reference_id:
            continue
        roles.append(
            ReferenceImageEvidence(
                reference_id=reference_id,
                role=_enum_value(item.get("role"), REFERENCE_IMAGE_ROLES, "CONTEXT_ONLY"),
                subject=string_value(item.get("subject")).strip(),
                allowed_uses=string_array(item.get("allowedUses"))[:5],
                forbidden_inferences=string_array(item.get("forbiddenInferences"))[:5],
                private_regions=_private_regions(item.get("privateRegions")),
                privacy_scanned=True,
            )
        )

    return ReferenceEvidenceProfile(
        has_references=True,
        has_user_experience_evidence=False,
        primary_entity=string_value(profile.get("primaryEntity")).strip() or None,
        brand=string_value(profile.get("brand")).strip() or None,
        product_category=string_value(profile.get("productCategory")).strip() or None,
        confirmed_attributes=string_array(profile.get("confirmedAttributes"))[:8],
        confirmed_use_scenes=string_array(profile.get("confirmedUseScenes"))[:6],
        reference_image_roles=roles[:4],
        source_facts=string_array(profile.get("sourceFacts"))[:8],
        forbidden_claims=string_array(profile.get("forbiddenClaims"))[:10],
        content_entity=content_entity_from_json(profile, raw_keyword),
    )


# 형용사만 있고 실행할 수 없는 답. 이런 값은 원고 단계에서 아무것도 바꾸지 못하면서
# 프롬프트만 길게 만들므로 버린다 — 빈 값이면 공통 규칙이 그 자리를 대신한다.
_EMPTY_DIRECTION_PHRASES = (
    "자연스럽게",
    "자연스러운",
    "읽기 좋게",
    "읽기 쉽게",
    "다양하게",
    "적절히",
    "적절하게",
    "친근하게",
    "전문적으로",
)


def _direction_value(value: Any) -> str:
    """편집 지시 한 항목. 실행할 수 없는 형용사뿐이면 빈 문자열로 떨어뜨린다."""
    text = " ".join(string_value(value).split())
    if not text or len(text) < 6:
        return ""
    stripped = text.rstrip(". ")
    # '자연스럽게 쓴다'처럼 형용사 하나로 끝나는 짧은 답만 걸러낸다. 같은 낱말이 긴 문장
    # 안에 들어 있는 것은 지시가 있다는 뜻이므로 그대로 둔다.
    if len(stripped) <= 20 and any(word in stripped for word in _EMPTY_DIRECTION_PHRASES):
        return ""
    return text[:300]


def writing_direction_from_json(value: Any) -> WritingDirection | None:
    """편집 지시 11항목. 항목 하나가 비어도 나머지는 살린다.

    전부 비었을 때만 None이다 — 그때는 원고 프롬프트가 공통 규칙만 쓴다.
    """
    if not isinstance(value, dict):
        return None
    direction = WritingDirection(
        voice_distance=_direction_value(value.get("voiceDistance")),
        reader_relationship=_direction_value(value.get("readerRelationship")),
        sentence_density=_direction_value(value.get("sentenceDensity")),
        opening_mode=_direction_value(value.get("openingMode")),
        rhythm_profile=_direction_value(value.get("rhythmProfile")),
        transition_style=_direction_value(value.get("transitionStyle")),
        detail_focus=_direction_value(value.get("detailFocus")),
        first_person_policy=_direction_value(value.get("firstPersonPolicy")),
        certainty_policy=_direction_value(value.get("certaintyPolicy")),
        closing_mode=_direction_value(value.get("closingMode")),
        avoid_patterns=[
            text for text in (t.strip() for t in string_array(value.get("avoidPatterns"))) if text
        ][:6],
    )
    filled = [
        text
        for text in direction.model_dump().values()
        if (text if isinstance(text, str) else "".join(text))
    ]
    return direction if filled else None


def editorial_style_plan_from_json(value: Any) -> EditorialStylePlan | None:
    """모델이 정한 카테고리·아키타입. 테마·팔레트·레이아웃 변형은 여기서 정하지 않는다 —
    그건 코드가 씨앗으로 고른다(modules/draft/editorial_style.normalize_style_plan).

    알 수 없는 값은 기본값으로 떨어뜨리고 계획 자체는 살린다. 카테고리 하나가 틀렸다고
    편집 계획 전체를 버리면, 그 글은 다시 '모든 글이 같은 파란 도표'로 돌아간다.
    """
    plan = value.get("editorialStylePlan") if isinstance(value, dict) else None
    if not isinstance(plan, dict):
        return None

    raw_budget = plan.get("visualBudget")
    raw_budget = raw_budget if isinstance(raw_budget, dict) else {}
    budget = VisualBudget(
        thumbnail=1,
        reference_images_max=_int_value(raw_budget.get("referenceImagesMax"), 2, high=2),
        body_photos_max=_int_value(raw_budget.get("bodyPhotosMax"), 3, high=4),
        rendered_visuals_max=_int_value(raw_budget.get("renderedVisualsMax"), 1, high=3),
    )

    return EditorialStylePlan(
        content_category=_enum_value(plan.get("contentCategory"), CONTENT_CATEGORIES, "OTHER"),
        editorial_archetype=_enum_value(
            plan.get("editorialArchetype"), EDITORIAL_ARCHETYPES, "EXPERT_EXPLAINER"
        ),
        voice_mode=_enum_value(plan.get("voiceMode"), VOICE_MODES, "DIRECT_EXPERT"),
        visual_density=_enum_value(plan.get("visualDensity"), VISUAL_DENSITY_LEVELS, "LOW"),
        emoji_level=_enum_value(plan.get("emojiLevel"), EMOJI_LEVELS, "NONE"),
        decoration_level=_enum_value(plan.get("decorationLevel"), DECORATION_LEVELS, "LOW"),
        article_rhythm=_enum_value(plan.get("articleRhythm"), ARTICLE_RHYTHMS, "ANSWER_FIRST"),
        body_highlight_style=_enum_value(
            plan.get("bodyHighlightStyle"), BODY_HIGHLIGHT_STYLES, "BOLD_ONLY"
        ),
        thumbnail_layout=_enum_value(
            plan.get("thumbnailLayout"), THUMBNAIL_LAYOUTS, "COPY_LEFT_SUBJECT_RIGHT"
        ),
        thumbnail_copy_mode=_enum_value(
            plan.get("thumbnailCopyMode"), THUMBNAIL_COPY_MODES, "SHORT_LABEL"
        ),
        visual_budget=budget,
        writing_direction=writing_direction_from_json(plan.get("writingDirection")),
    )


def card_plan_from_json(value: Any) -> VisualCardPlan | None:
    """저장 호환 사진 계획 파싱.

    신규 도구 스키마에는 카드뉴스 문구·디자인 시스템이 없지만 구형 응답의 필드도 계속
    읽는다. 필요성 게이트·articleClaim 대조·최대 장수는 서비스 선정 단계가 강제한다.
    """
    if not isinstance(value, dict):
        return None

    raw_design = value.get("designSystem")
    raw_design = raw_design if isinstance(raw_design, dict) else {}
    design = CardDesignSystem.model_validate(raw_design)

    raw_cards = value.get("cards")
    if not isinstance(raw_cards, list):
        return None

    cards: list[CardBrief] = []
    for item in raw_cards[:6]:
        if not isinstance(item, dict):
            continue
        card_type = string_value(item.get("cardType")).strip().upper()
        claim = string_value(item.get("articleClaim")).strip()
        headline = [line.strip() for line in string_array(item.get("headlineLines")) if line.strip()]
        raw_scene = item.get("scene")
        if card_type not in CARD_TYPES or not claim:
            continue
        if not isinstance(raw_scene, dict):
            continue
        scene = CardScene.model_validate(raw_scene)
        if not scene.main_subject.strip():
            continue
        icon = string_value(item.get("iconType")).strip().lower()
        subject_kind = _enum_value(
            item.get("subjectKind"), VISUAL_SUBJECT_KINDS, "NON_PERSON"
        )
        must_show = item.get("mustShowSubject") is True
        # 고유한 이름을 가진 대상은 모델이 false로 내려도 코드가 True로 못 박는다.
        # 여기서 놓치면 스파이더맨 글이 거미줄 사진으로 끝난다(CardBrief가 한 번 더 강제한다).
        if subject_kind in NAMED_SUBJECT_KINDS:
            must_show = True
        try:
            card = CardBrief(
                card_id=string_value(item.get("cardId")).strip() or f"card-{len(cards) + 1}",
                card_type=card_type,
                section_id=string_value(item.get("sectionId")).strip() or None,
                section_heading=string_value(item.get("sectionHeading")).strip() or None,
                article_claim=claim,
                visual_purpose=string_value(item.get("visualPurpose")).strip(),
                eyebrow=string_value(item.get("eyebrow")).strip(),
                headline_lines=headline[:2],
                emphasis_words=string_array(item.get("emphasisWords"))[:2],
                summary_lines=[
                    line.strip()
                    for line in string_array(item.get("summaryLines"))
                    if line.strip()
                ][:2],
                icon_type=icon if icon in CARD_ICON_TYPES else "info",
                scene=scene,
                search_queries=string_array(item.get("searchQueries"))[:4],
                visual_reference_summary=(
                    string_value(item.get("visualReferenceSummary")).strip() or None
                ),
                alt_text=string_value(item.get("altText")).strip() or None,
                caption=string_value(item.get("caption")).strip() or None,
                necessity_score=_score_value(item.get("necessityScore")),
                # 참고 이미지가 없으면 프롬프트에 표시가 없어 모델도 false로 둔다. 값이 빠지면
                # 안전하게 false(일반 생성).
                uses_reference=item.get("usesReferenceImage") is True,
                reference_id=string_value(item.get("referenceId")).strip() or None,
                photo_role=_enum_value(item.get("photoRole"), PHOTO_ROLES, "IN_USE_SCENE"),
                # 구도는 여기서 받기만 하고, 역할·썸네일 여부에 맞는 정규화는 CardBrief가
                # 한다(normalized_framing) — 저장된 옛 계획도 같은 규칙으로 읽히게 한다.
                visual_subject=string_value(item.get("visualSubject")).strip(),
                framing=_enum_value(item.get("framing"), PHOTO_FRAMINGS, "MEDIUM"),
                # 값이 빠지거나 모르는 값이면 빈 문자열 — 코드 기본 사다리(네이버→생성).
                image_source=_enum_value(item.get("imageSource"), IMAGE_SOURCES, ""),
                subject_identity=string_value(item.get("subjectIdentity")).strip() or None,
                subject_kind=subject_kind,
                must_show_subject=must_show,
                identity_confidence=_confidence_value(item.get("identityConfidence")),
                product_fidelity_requirements=string_array(
                    item.get("productFidelityRequirements")
                )[:5],
                section_claim=string_value(item.get("sectionClaim")).strip() or None,
                visual_continuity=string_value(item.get("visualContinuity")).strip() or None,
                generated_or_reused=_enum_value(
                    item.get("generatedOrReused"), PHOTO_SOURCE_MODES, "GENERATED"
                ),
                forbidden_inference=string_array(item.get("forbiddenInference"))[:5],
            )
        except ValueError:
            # 실존 인물이라면서 이름을 비워 온 카드. 이름 없이 만들 수 있는 그림은
            # '그 직업의 아무나'뿐이라 그 카드만 버린다 — 계획 전체를 죽이지는 않는다.
            continue
        cards.append(card)

    if not cards:
        return None
    return VisualCardPlan(design_system=design, cards=cards)


# 유형별로 쓸 수 있는 배치 변형. 표에 과정도 변형이 붙는 것 같은 어긋난 조합은 버린다 —
# 렌더러가 알아보지 못하면 조용히 기본값으로 그려져 "계획이 반영되지 않았다"가 된다.
_LAYOUT_VARIANTS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "TABLE": TABLE_VARIANTS,
    "PROCESS_DIAGRAM": PROCESS_VARIANTS,
    "INFOGRAPHIC": INFOGRAPHIC_VARIANTS,
    "BAR_CHART": ("VERTICAL_BAR", "HORIZONTAL_BAR"),
}

# 옛 저장 데이터의 프리셋 이름 → 새 테마. 예전 글을 다시 그려도 그림이 바뀌지 않는다.
_LEGACY_STYLE_ALIASES = {
    "LIFESTYLE_SOFT": "LIFESTYLE_JOURNAL",
    "TECH_MINIMAL": "TECH_BENCHMARK_LIGHT",
    "PROFESSIONAL_DATA": "FINANCE_REPORT",
}


def _visual_style_value(value: Any) -> str | None:
    raw = string_value(value).strip().upper()
    raw = _LEGACY_STYLE_ALIASES.get(raw, raw)
    return raw if raw in VISUAL_THEMES else None


def planned_visuals_from_json(value: Any) -> list[PlannedVisual]:
    """모델이 반환한 코드 렌더링 시각자료 데이터. 형식이 깨진 항목은 버린다 — 검증(수치·
    출처 유무)은 렌더링 직전에 visuals 모듈이 한다."""
    if not isinstance(value, list):
        return []
    visuals: list[PlannedVisual] = []
    for index, item in enumerate(value[:4]):
        if not isinstance(item, dict):
            continue
        visual_type = string_value(item.get("type")).strip().upper()
        title = string_value(item.get("title")).strip()
        if not visual_type or not title:
            continue
        if visual_type == "TABLE":
            raw_columns = item.get("columns")
            raw_rows = item.get("rows")
            # 도구 스키마의 상한을 벗어난 표를 먼저 잘라 정상 표처럼 보이게 만들지 않는다.
            # 5번째 열·6번째 행·5번째 셀을 조용히 잃는 것보다 해당 visual 전체를 제외해
            # 본문 마커만 걷어내는 편이 안전하다.
            if isinstance(raw_columns, list) and len(raw_columns) > 4:
                continue
            if isinstance(raw_rows, list):
                if len(raw_rows) > 5:
                    continue
                if any(
                    isinstance(row, dict)
                    and isinstance(row.get("cells"), list)
                    and len(row["cells"]) > 4
                    for row in raw_rows
                ):
                    continue

        data: list[VisualDataPoint] | None = None
        if isinstance(item.get("data"), list):
            data = []
            for point in item["data"][:12]:
                if not isinstance(point, dict):
                    continue
                label = string_value(point.get("label")).strip()
                raw = point.get("value")
                if not label or isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                data.append(VisualDataPoint(label=label, value=float(raw)))
            data = data or None

        steps: list[VisualStep] | None = None
        if isinstance(item.get("steps"), list):
            steps = []
            for step in item["steps"][:6]:
                # 예전 형식(문자열 단계)도 그대로 받는다 — 저장된 원고와 모델 변덕 양쪽에 있다.
                if isinstance(step, str):
                    label, detail = step.strip(), ""
                elif isinstance(step, dict):
                    label = string_value(step.get("label")).strip()
                    detail = string_value(step.get("detail")).strip()
                else:
                    continue
                if label:
                    steps.append(VisualStep(label=label, detail=detail or None))
            steps = steps or None

        rows: list[VisualTableRow] | None = None
        if isinstance(item.get("rows"), list):
            rows = []
            for row in item["rows"][:5]:
                if not isinstance(row, dict):
                    continue
                name = string_value(row.get("name")).strip()
                cells = string_array(row.get("cells"))[:4]
                if name and cells:
                    rows.append(VisualTableRow(name=name, cells=cells))
            rows = rows or None

        groups: list[VisualGroup] | None = None
        if isinstance(item.get("groups"), list):
            groups = []
            for group in item["groups"][:4]:
                if not isinstance(group, dict):
                    continue
                name = string_value(group.get("name")).strip()
                items = string_array(group.get("items"))[:4]
                if name and items:
                    groups.append(VisualGroup(name=name, items=items))
            groups = groups or None

        visuals.append(
            PlannedVisual(
                visual_id=string_value(item.get("visualId")).strip() or f"visual-{index + 1}",
                type=visual_type,
                title=title,
                caption=string_value(item.get("caption")).strip() or None,
                alt_text=string_value(item.get("altText")).strip() or None,
                section_id=string_value(item.get("sectionId")).strip() or None,
                data=data,
                unit=string_value(item.get("unit")).strip() or None,
                x_axis_label=string_value(item.get("xAxisLabel")).strip() or None,
                y_axis_label=string_value(item.get("yAxisLabel")).strip() or None,
                conclusion=string_value(item.get("conclusion")).strip() or None,
                steps=steps,
                center_topic=string_value(item.get("centerTopic")).strip() or None,
                groups=groups,
                columns=string_array(item.get("columns"))[:4] or None,
                rows=rows,
                source=string_value(item.get("source")).strip() or None,
                published_at=string_value(item.get("publishedAt")).strip() or None,
                style=_visual_style_value(item.get("style")),
                layout_variant=_enum_value(
                    item.get("layoutVariant"),
                    _LAYOUT_VARIANTS_BY_TYPE.get(visual_type, ()),
                    "",
                )
                or None,
                highlight_labels=string_array(item.get("highlightLabels"))[:3],
                density=string_value(item.get("density")).strip().upper() or None,
                visual_reason=string_value(item.get("visualReason")).strip() or None,
                necessity_score=_score_value(item.get("necessityScore")),
            )
        )
    return visuals


def apply_visual_theme(visuals: list[PlannedVisual], theme: str | None) -> list[PlannedVisual]:
    """한 글의 시각자료를 하나의 테마로 통일한다.

    자료마다 다른 테마를 고르면 같은 글 안에서 도표 세 개가 서로 다른 색이 된다 — 글끼리
    달라야지 자료끼리 달라서는 안 된다. 배치 변형은 자료마다 달라도 좋으므로 건드리지 않는다.
    """
    if not theme or theme not in VISUAL_THEMES:
        return visuals
    return [visual.model_copy(update={"style": theme}) for visual in visuals]


def dedupe_sources(sources: list[SearchSource]) -> list[SearchSource]:
    seen: set[str] = set()
    result = []
    for source in sources:
        key = source.url or source.title
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def markdown_from_post(title: str, body: str) -> str:
    return f"# {title}\n\n{body}"


def html_from_post(title: str, body: str) -> str:
    paragraphs = "".join(
        f"<p>{escape_html(line.strip())}</p>" for line in re.split(r"\n{2,}", body)
    )
    return f"<article><h1>{escape_html(title)}</h1>{paragraphs}</article>"


# Python의 re에서 \w는 이미 유니코드를 인식하므로 [^\w\s]는 원본의
# /[^\p{L}\p{N}\s]/gu와 같다(_를 추가로 허용하지만 문제없다).
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def hashtag_seeds(value: str) -> list[str]:
    compact = _NON_WORD.sub(" ", value).strip()
    words = [word for word in re.split(r"\s+", compact) if len(word) > 1]
    primary = words[0] if words else re.sub(r"\s+", "", compact)
    if not primary:
        return []
    return [primary, f"{primary}정보", f"{primary}가이드", f"{primary}입문", f"{primary}팁"]


def normalize_hashtags(raw: list[str], count: int, fallback_keywords: list[str]) -> list[str]:
    seed_tags = [tag for keyword in fallback_keywords for tag in hashtag_seeds(keyword)]
    candidates = [
        re.sub(r"\s+", "", tag.removeprefix("#")).strip()
        for tag in [*raw, *fallback_keywords, *seed_tags]
    ]

    seen: set[str] = set()
    normalized: list[str] = []
    for tag in candidates:
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
        if len(normalized) >= count:
            break
    return normalized


def text_from_html(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<style[\s\S]*?</style>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"</(p|div|li|h[1-6]|section|article)>", "\n\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    for entity, char in [
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ]:
        value = value.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def text_from_markdown(value: str) -> str:
    value = re.sub(r"!\[[^\]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[[^\]]*]\([^)]*\)", "", value)
    # 스티커 자리 표식(네이버 발행 전용, 2026-08-10). 아래의 기호 걷기는 `[`를 남기므로
    # 여기서 지우지 않으면 순수 텍스트 본문에 [[STICKER: …]]가 글자로 샌다.
    value = re.sub(r"\[\[\s*STICKER\s*[:：][^\]]*\]\]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^#{1,6}\s+", "", value, flags=re.MULTILINE)
    # `=`도 걷는다: 형광펜(==) 문법이 남으면 본문 텍스트에 기호가 샌다.
    value = re.sub(r"[*_`>~=-]", "", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _body_from_markdown(markdown: str, title: str) -> str:
    """마크다운 원고에서 순수 텍스트 본문을 유도한다. 첫 `# 제목` 줄은 본문이 아니다 —
    남겨 두면 제목이 본문 글자수에 끼고 화면에 두 번 찍힌다."""
    without_title = re.sub(rf"^#\s+{re.escape(title)}\s*\n+", "", markdown.strip())
    if without_title == markdown.strip():
        without_title = re.sub(r"^#\s+[^\n]*\n+", "", markdown.strip(), count=1)
    return text_from_markdown(without_title)


def final_post_from_json(
    value: Any,
    fallback_title: str,
    hashtag_count: int,
    fallback_keywords: list[str],
    forced_title: str | None = None,
) -> FinalPost:
    """forced_title은 원고보다 먼저 확정된 제목(TitlePlan.primary_title)이다.

    주면 모델이 무엇을 반환했든 그 제목이 이긴다. 제목을 되물어 볼 이유가 없기 때문이다 —
    이미 정해진 값이고, 모델이 어겼다고 원고 전체를 다시 생성하면 멀쩡한 본문을 버리는
    셈이다. markdown_to_html·markdown_for_storage가 이 제목으로 H1을 다시 세우므로
    제목과 H1이 어긋날 수 없다.
    """
    source = value if isinstance(value, dict) else {}
    title = (forced_title or "").strip() or string_value(source.get("title"), fallback_title)
    html_content = string_value(source.get("htmlContent"))
    markdown_content = string_value(source.get("markdownContent"))

    # 새 규격: 모델은 markdownContent 한 벌만 쓴다(DRAFT_SCHEMA). HTML·텍스트는 코드가
    # 유도한다 — 같은 원고를 세 번 출력하게 하던 토큰 낭비를 없앤다. 예전 형식(모델이
    # body·htmlContent를 직접 준 저장분·구형 응답)은 그대로 받는다.
    if markdown_content and not html_content:
        html_content = markdown_to_html(title, markdown_content)
        markdown_content = markdown_for_storage(title, markdown_content)

    body = string_value(
        source.get("body"),
        string_value(
            source.get("bodyMarkdown"),
            (
                _body_from_markdown(markdown_content, title)
                if markdown_content
                else text_from_html(html_content)
            ),
        ),
    )
    hashtags = normalize_hashtags(
        string_array(source.get("hashtags")), hashtag_count, fallback_keywords
    )

    return FinalPost(
        title=title,
        body=body,
        hashtags=hashtags,
        # 렌더러로 나갈 때가 아니라 들어올 때 정규화한다: 이 문구는 UI에도 보이는데,
        # 렌더 시점에만 잘리는 20자짜리 줄은 화면과 썸네일이 어긋나게 만든다.
        thumbnail_copy=thumbnail_lines(string_array(source.get("thumbnailCopy")), title),
        html_content=html_content or html_from_post(title, body),
        markdown_content=markdown_content or markdown_from_post(title, body),
    )
