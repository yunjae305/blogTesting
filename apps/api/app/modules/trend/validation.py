"""트렌드/주제 요청 본문 검증."""

from dataclasses import dataclass, field
from typing import Any

from app.errors import BlogTaskError
from app.shared import TitleHookType, TrendMode, TrendSource

# 화면이 상위 8개를 먼저 보이고 "더 보기"로 나머지를 누적 노출하므로, 한 번에 받는
# 후보 상한을 넉넉히 둔다. (선택하는 키워드 수가 아니라 한 화면에서 비교하는 후보 수)
MAX_TREND_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 100
# 이미 화면에 있는 제목들. 다시 생성할 때 반복하지 않도록 보낸다. 클라이언트가 정하고
# 모든 항목이 프롬프트에 들어가므로 상한을 둔다.
MAX_EXCLUDE_TITLES = 30
MAX_TITLE_LENGTH = 200
# '제목 추천 다시'를 누른 회차. 코드가 이 값으로 재생성 방향을 고르므로(title_variation) 큰
# 값은 의미가 없다 — 방향 표가 7개라 그 이상은 같은 자리를 돈다. 상한은 방어일 뿐이다.
MAX_REGENERATION_COUNT = 50
# 새로고침이 같은 것을 다시 내놓지 않도록 클라이언트가 보내는, 최근에 보여준 키워드들.
# 클라이언트가 정하므로 상한을 둔다. 어차피 새로고침 몇 번 정도다.
MAX_EXCLUDE_KEYWORDS = 60
# 소재 관련순 '다른 후보 보기'가 서버에서 받아 그대로 돌려주는 커서. 서버가 만든 불투명
# 문자열이라 내용은 검증하지 않고 길이만 막는다 — 클라이언트가 해석하지 않는 값이다.
MAX_CURSOR_LENGTH = 200
# 문맥 키(materialKey|카테고리|하위카테고리)의 최대 길이. 서버가 만든 불투명 값이라
# 내용은 검증하지 않고 길이만 막는다.


@dataclass
class TrendRecommendationRequest:
    mode: TrendMode = TrendMode.TRENDING
    country: str | None = None
    category: str | None = None
    max_keywords: int | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    # 수집하기: 캐시가 신선해도 소스를 다시 불러 새 키워드를 풀에 합친다.
    force_collect: bool = False
    # 최신순 '다른 후보 보기': 노출 이력을 무시하고 저장된 풀 전체에서 무작위로 뽑는다.
    # 이력 제외 방식은 풀을 한 바퀴 돌면 후보가 말라붙어 버튼이 죽는다 — 중복 노출을
    # 허용하는 대신 언제 눌러도 다른 조합이 나온다.
    shuffle: bool = False
    # 소재 관련순 '다른 후보 보기'의 위치. 서버가 nextCursor로 내려준 값을 그대로 보낸다.
    # exclude 누적 방식과 달리 후보가 말라붙지 않는다 — 끝에 닿으면 순환하기 때문이다.
    cursor: str | None = None


@dataclass
class TopicGenerationRequest:
    trend_keyword_id: str
    keyword: str
    source: TrendSource
    exclude_titles: list[str] = field(default_factory=list)
    # 직전 후보가 쓴 관점(제목 + 후킹 유형 + 기본 유형). 재생성이 문장만 바꾸는 것을 막는다.
    # 옛 클라이언트는 보내지 않으므로 비어 있을 수 있다.
    exclude_angles: list[dict] = field(default_factory=list)
    # '제목 추천 다시'를 몇 번째 누른 것인지. 코드가 이 값으로 이번 회차의 방향을 고른다.
    regeneration_count: int = 0


@dataclass
class SelectTrendTopicRequest:
    final_topic: str
    skipped: bool
    topic_candidate_id: str | None = None
    selected_trend_keyword_ids: list[str] = field(default_factory=list)
    # 고른 키워드의 문자열. 옛 클라이언트는 보내지 않으므로 비어 있을 수 있다.
    selected_keywords: list[str] = field(default_factory=list)
    hook_type: TitleHookType | None = None


def _validate_string_array(value: Any, name: str, max_items: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BlogTaskError("VALIDATION_FAILED", f"{name} must be an array")
    if len(value) > max_items:
        raise BlogTaskError("VALIDATION_FAILED", f"{name} must have at most {max_items} items")

    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BlogTaskError("VALIDATION_FAILED", f"{name}[{index}] must be a non-empty string")
        result.append(item.strip())
    return result


def validate_trend_recommendation_request(body: Any) -> TrendRecommendationRequest:
    if body is None:
        return TrendRecommendationRequest()
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    raw_mode = body.get("mode")
    try:
        mode = TrendMode(raw_mode) if raw_mode is not None else TrendMode.TRENDING
    except ValueError:
        modes = ", ".join(item.value for item in TrendMode)
        raise BlogTaskError(
            "VALIDATION_FAILED", f"mode must be one of {modes}, received {raw_mode!r}"
        ) from None

    country = body.get("country")
    category = body.get("category")
    max_keywords = body.get("maxKeywords")

    if country is not None and (not isinstance(country, str) or not country.strip()):
        raise BlogTaskError(
            "VALIDATION_FAILED", "country must be a non-empty string when provided"
        )
    if category is not None and (not isinstance(category, str) or not category.strip()):
        raise BlogTaskError(
            "VALIDATION_FAILED", "category must be a non-empty string when provided"
        )
    if max_keywords is not None and (
        not isinstance(max_keywords, int)
        or isinstance(max_keywords, bool)
        or not 1 <= max_keywords <= MAX_TREND_KEYWORDS
    ):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"maxKeywords must be an integer between 1 and {MAX_TREND_KEYWORDS}",
        )

    exclude_keywords = _validate_string_array(
        body.get("excludeKeywords"), "excludeKeywords", MAX_EXCLUDE_KEYWORDS
    )

    force_collect = body.get("forceCollect")
    if force_collect is not None and not isinstance(force_collect, bool):
        raise BlogTaskError(
            "VALIDATION_FAILED", "forceCollect must be a boolean when provided"
        )

    shuffle = body.get("shuffle")
    if shuffle is not None and not isinstance(shuffle, bool):
        raise BlogTaskError("VALIDATION_FAILED", "shuffle must be a boolean when provided")

    cursor = body.get("cursor")
    if cursor is not None and (
        not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH
    ):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"cursor must be a string of at most {MAX_CURSOR_LENGTH} characters",
        )

    return TrendRecommendationRequest(
        mode=mode,
        country=country.strip() if isinstance(country, str) else None,
        category=category.strip() if isinstance(category, str) else None,
        max_keywords=max_keywords,
        exclude_keywords=exclude_keywords,
        force_collect=force_collect is True,
        shuffle=shuffle is True,
        cursor=cursor.strip() if isinstance(cursor, str) and cursor.strip() else None,
    )


def _required_string(body: dict, name: str, max_length: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError("VALIDATION_FAILED", f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{name} must be at most {max_length} characters"
        )
    return value


def validate_topic_generation_request(body: Any) -> TopicGenerationRequest:
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    # 추천이 읽기 전용이라 키워드는 클라이언트에서 온다 — 서버가 id로 찾아볼 키워드가
    # 어디에도 저장되지 않는다.
    trend_keyword_id = _required_string(body, "trendKeywordId", MAX_KEYWORD_LENGTH)
    keyword = _required_string(body, "keyword", MAX_KEYWORD_LENGTH)

    raw_source = body.get("source")
    try:
        source = TrendSource(raw_source) if raw_source is not None else TrendSource.GOOGLE_TRENDS
    except ValueError:
        sources = ", ".join(item.value for item in TrendSource)
        raise BlogTaskError(
            "VALIDATION_FAILED", f"source must be one of {sources}, received {raw_source!r}"
        ) from None

    exclude_titles = _validate_string_array(
        body.get("excludeTitles"), "excludeTitles", MAX_EXCLUDE_TITLES
    )
    for index, title in enumerate(exclude_titles):
        if len(title) > MAX_TITLE_LENGTH:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"excludeTitles[{index}] must be at most {MAX_TITLE_LENGTH} characters",
            )

    exclude_angles = _validate_exclude_angles(body.get("excludeAngles"))
    regeneration_count = _validate_regeneration_count(body.get("regenerationCount"))

    return TopicGenerationRequest(
        trend_keyword_id=trend_keyword_id,
        keyword=keyword,
        source=source,
        exclude_titles=exclude_titles,
        exclude_angles=exclude_angles,
        regeneration_count=regeneration_count,
    )


def _validate_exclude_angles(raw: Any) -> list[dict]:
    """직전 후보의 관점 목록. 제목만 필수이고 후킹·유형은 없으면 없는 대로 둔다 —
    옛 클라이언트와 옛 화면 상태를 거절하지 않는다."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BlogTaskError("VALIDATION_FAILED", "excludeAngles must be an array")
    if len(raw) > MAX_EXCLUDE_TITLES:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"excludeAngles must have at most {MAX_EXCLUDE_TITLES} items"
        )
    angles: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BlogTaskError("VALIDATION_FAILED", f"excludeAngles[{index}] must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise BlogTaskError(
                "VALIDATION_FAILED", f"excludeAngles[{index}].title must be a non-empty string"
            )
        if len(title) > MAX_TITLE_LENGTH:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"excludeAngles[{index}].title must be at most {MAX_TITLE_LENGTH} characters",
            )
        angle: dict = {"title": title.strip()}
        for key in ("hookType", "titleType"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                angle[key] = value.strip()[:MAX_KEYWORD_LENGTH]
        angles.append(angle)
    return angles


def _validate_regeneration_count(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BlogTaskError("VALIDATION_FAILED", "regenerationCount must be an integer")
    if raw < 0:
        raise BlogTaskError("VALIDATION_FAILED", "regenerationCount must not be negative")
    # 상한을 두는 이유는 방어다 — 회차 값은 방향 회전에만 쓰이므로 큰 값이 의미를 갖지 않는다.
    return min(raw, MAX_REGENERATION_COUNT)


def validate_select_trend_topic_request(body: Any, fallback_topic: str) -> SelectTrendTopicRequest:
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    topic_candidate_id = body.get("topicCandidateId")
    final_topic = body.get("finalTopic")
    skipped = body.get("skipped")
    is_skipped = skipped is True

    if topic_candidate_id is not None and (
        not isinstance(topic_candidate_id, str) or not topic_candidate_id.strip()
    ):
        raise BlogTaskError(
            "VALIDATION_FAILED", "topicCandidateId must be a non-empty string when provided"
        )
    if not is_skipped and (not isinstance(final_topic, str) or not final_topic.strip()):
        raise BlogTaskError(
            "VALIDATION_FAILED", "finalTopic is required when trend recommendation is used"
        )
    if skipped is not None and not isinstance(skipped, bool):
        raise BlogTaskError("VALIDATION_FAILED", "skipped must be a boolean when provided")

    # 고른 제목의 후킹 유형. 클라이언트가 받은 후보의 값을 그대로 되돌려 보낸다. 모르는
    # 값·구버전 클라이언트의 누락은 오류로 만들지 않고 None으로 둔다 — 이 값이 없다고 제목
    # 선택이 실패하면, 부가 정보 하나 때문에 사용자의 진행이 막히는 셈이다.
    raw_hook_type = body.get("hookType")
    try:
        hook_type = TitleHookType(raw_hook_type) if isinstance(raw_hook_type, str) else None
    except ValueError:
        hook_type = None

    return SelectTrendTopicRequest(
        # 건너뛰면 1단계에서 사용자가 입력한 주제를 유지한다.
        final_topic=fallback_topic if is_skipped else final_topic.strip(),
        skipped=is_skipped,
        # 트렌드를 건너뛴 글에는 고른 제목이 없으므로 후킹 유형도 없다.
        hook_type=None if is_skipped else hook_type,
        topic_candidate_id=(
            topic_candidate_id.strip() if isinstance(topic_candidate_id, str) else None
        ),
        selected_trend_keyword_ids=_validate_string_array(
            body.get("selectedTrendKeywordIds"), "selectedTrendKeywordIds", MAX_TREND_KEYWORDS
        ),
        # 건너뛴 글에는 고른 키워드가 없다.
        selected_keywords=(
            []
            if is_skipped
            else _validate_string_array(
                body.get("selectedKeywords"), "selectedKeywords", MAX_TREND_KEYWORDS
            )
        ),
    )
