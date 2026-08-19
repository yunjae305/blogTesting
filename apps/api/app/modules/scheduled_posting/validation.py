"""예약 시작 요청의 검증.

소재 정리(빈 줄 제거·앞뒤 공백·중복 제거)를 서비스가 아니라 여기서 한다 — 화면과
서버가 같은 규칙으로 세어야 '총 N개 소재'와 실제로 만들어지는 작업 수가 어긋나지 않는다.

소재 길이는 기존 새 글 작성과 **같은 한도**(MAX_TOPIC_CHARS)를 쓴다. 예약만 더 긴 소재를
받아 두면 create_blog_task가 뒤늦게 거부해, 이미 만들어진 배치 안에서 실패한다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.errors import BlogTaskError
from app.modules.blog_task.validation import MAX_TOPIC_CHARS
from app.shared import SUBJECT_CATEGORIES

from .models import ScheduleMode, SchedulePlatform, ScheduleTopicMode

#: 한 번에 예약할 수 있는 최대 작업 수.
MAX_SCHEDULED_TOPICS = 20

#: 발행 간격(초). 15초 미만은 네이버가 연속 게시로 볼 위험이 크고, 한 시간을 넘기면
#: 예약이라기보다 방치에 가깝다. 화면은 '분 + 초'로 받아 초로 환산해 보낸다.
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 3600

MAX_CLIENT_REQUEST_ID_CHARS = 200

#: 지금보다 이만큼 앞선 시각까지는 '과거'로 보지 않는다. 사용자의 시계와 서버 시계가
#: 몇 초 어긋나 있을 수 있고, 요청이 오가는 데도 시간이 걸린다 — 방금 고른 '1분 뒤'를
#: 시계 차이 때문에 거부하면 사용자는 이유를 알 수 없다.
PAST_PUBLISH_GRACE_SECONDS = 60

#: 이보다 먼 미래는 받지 않는다. 1년 뒤 예약은 관리할 수 없는 약속이고, 오타(2126년)를
#: 걸러내는 그물이기도 하다.
MAX_PUBLISH_HORIZON_DAYS = 365

#: 절대 시각 예약에서 **글과 글 사이의 최소 간격**(2026-08-07 사용자 결정).
#:
#: 발행은 한 번에 하나씩만 돈다 — 같은 사용자 프로필로 크롬을 두 개 띄우면 프로필
#: 잠금 때문에 발행이 통째로 실패한다(worker.py 참고). 그래서 앞 글의 발행이 끝나야
#: 뒤 글이 시작되고, 간격이 그보다 좁으면 뒤 글은 반드시 예약 시각을 넘긴다.
#:
#: 실측(2026-08-07, 저장된 작업 기록에서):
#:
#: - 발행 동작 자체: 네이버 26~35초, 스레드 1분 32초~2분 32초(최악 8분 10초)
#: - 원고 생성: 중앙값 6분 27초(최소 5분 04초, 최대 9분 27초)
#:
#: 10분은 최악의 발행 시간까지 삼키는 값이다. 이보다 촘촘한 예약은 받아 봐야 지킬 수
#: 없는 약속이라, 저장을 거부하고 이유를 알려 준다.
MIN_PUBLISH_GAP_SECONDS = 12 * 60

#: 시간대 이름의 최대 길이. IANA 이름은 "America/Argentina/ComodRivadavia"가 최장이다.
MAX_TIMEZONE_CHARS = 64

#: 예약 작업이 만드는 BlogTask의 목적. 새 글 작성 화면에서는 사용자가 직접 고르는 값인데,
#: 예약에는 그 화면이 없다. 기존 원고 프롬프트나 일반 새 글 작성의 검증은 이 상수 때문에
#: 바뀌지 않는다 — 그냥 create_blog_task가 요구하는 purpose 자리를 채울 뿐이다.
#:
#: **문자열을 바꾸면 화면도 함께 고쳐야 한다.** 사용자가 고른 목적이 아니므로 '내 글 목록'과
#: 검증 팝업은 이 값을 걸러 낸다(apps/web/src/constants.ts의 SCHEDULED_DEFAULT_PURPOSE).
#: 두 값이 어긋나면 예약 글에 이 문장이 다시 보인다.
SCHEDULED_DEFAULT_PURPOSE = ["소재에 맞는 유용한 정보 제공 및 검색 의도 충족"]


@dataclass
class PlannedPublish:
    """글 하나의 예약 — 소재와 **그 글이 올라갈 절대 시각**."""

    topic: str
    #: UTC ISO 문자열. 간격 방식이면 None이다.
    publish_at: str | None = None
    #: 이 글을 네이버에 올릴지. 지정하지 않으면(None) 배치 기본값을 따른다.
    publish_naver: bool | None = None
    #: 이 글을 스레드에 올릴지. 지정하지 않으면 배치 기본값을 따른다.
    publish_threads: bool | None = None
    #: 이 소재가 **어느 분야의 것인가**(SUBJECT_CATEGORIES 중 하나, 2026-08-12).
    #:
    #: 새 글 작성에서 사용자가 직접 고르는 값과 같은 것이다 — '오디세이'가 영화인지
    #: 게임인지 모니터인지를 가른다. **선택이다**: 보내지 않으면(None) 예전 그대로
    #: 모델이 소재 글자만 보고 판단한다.
    subject_category: str | None = None


@dataclass
class StartScheduledBatchRequest:
    topics: list[str]
    target_count: int
    interval_seconds: int
    platform: SchedulePlatform
    topic_mode: ScheduleTopicMode = ScheduleTopicMode.MULTI
    #: 배치의 기본 게시 대상. 기본은 예전 동작(네이버만)이다. 쓰레드 단독 예약은
    #: publish_naver=False, publish_threads=True로 온다(2026-08-06).
    publish_naver: bool = True
    publish_threads: bool = False
    client_request_id: str | None = None
    #: 이 배치의 글에 활용할 브랜드(2026-08-19). 배치 전체에 하나다 — 자동 포스팅으로
    #: 거는 큐는 성격이 "이 소재들을 우리 서비스와 엮어 쓴다"라, 줄마다 다른 브랜드를
    #: 고르는 일이 없다. 선택이며, 없으면 예전 그대로 브랜드 없는 글이 나간다.
    brand_id: str | None = None
    #: 정리 과정에서 실제로 버린 것들. 화면에 그대로 알려 주기 위한 것이다.
    dropped_duplicates: list[str] = field(default_factory=list)
    #: 발행 시점을 정하는 방식. schedules를 보냈으면 ABSOLUTE다.
    schedule_mode: ScheduleMode = ScheduleMode.INTERVAL
    #: 글별 예약(ABSOLUTE일 때만 채워진다). topics와 같은 순서다.
    schedules: list[PlannedPublish] = field(default_factory=list)
    #: 사용자가 시각을 고른 시간대(IANA). 표시용으로만 저장한다.
    timezone: str | None = None


@dataclass
class RescheduleJobRequest:
    """예약 하나의 변경 요청. 보내지 않은 항목은 None이고, 그 값은 건드리지 않는다."""

    publish_at: str | None = None
    timezone: str | None = None
    publish_naver: bool | None = None
    publish_threads: bool | None = None


def parse_publish_at(value: Any, *, field_name: str = "publishAt") -> datetime:
    """ISO 8601 문자열 → 시간대를 아는 datetime(UTC).

    **오프셋이 없는 문자열은 거부한다.** "2026-08-06T15:00"만 받으면 그것이 서울의 3시인지
    런던의 3시인지 서버는 알 수 없고, 서버 시간대로 짐작하는 순간 사용자가 고른 시각과
    다른 때에 글이 올라간다. 클라이언트는 자기 시간대의 오프셋을 붙여 보낸다.
    """
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError("VALIDATION_FAILED", f"{field_name}는 ISO 8601 문자열이어야 합니다.")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{field_name} 형식을 읽을 수 없습니다: {text}"
        ) from error
    if parsed.tzinfo is None:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"{field_name}에는 시간대 오프셋이 있어야 합니다(예: 2026-08-06T15:00:00+09:00).",
        )
    return parsed.astimezone(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """저장 형식. 프로젝트의 다른 시각 필드와 같은 모양이다(밀리초 + Z)."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def validate_publish_at(
    value: Any, *, now: datetime | None = None, field_name: str = "publishAt"
) -> str:
    """발행 시각 하나를 검증하고 UTC ISO 문자열로 돌려준다.

    지난 시각을 막는 것이 핵심이다. 워커는 '시각이 지났으면 지금 올린다'로 도는데,
    과거를 그대로 받으면 저장하자마자 발행된다 — 사용자가 원한 것은 예약이지 즉시
    발행이 아니다(즉시 발행은 글 화면의 발행 버튼이 따로 있다).
    """
    moment = now or datetime.now(timezone.utc)
    parsed = parse_publish_at(value, field_name=field_name)
    if parsed < moment - timedelta(seconds=PAST_PUBLISH_GRACE_SECONDS):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            "이미 지난 시각으로는 예약할 수 없습니다. 지금보다 뒤의 시각을 골라 주세요.",
        )
    if parsed > moment + timedelta(days=MAX_PUBLISH_HORIZON_DAYS):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"예약은 최대 {MAX_PUBLISH_HORIZON_DAYS}일 뒤까지만 걸 수 있습니다.",
        )
    return to_utc_iso(parsed)


def validate_timezone(value: Any) -> str | None:
    """시간대 이름. 값이 없으면 None.

    **이름으로 시각을 계산하지 않는다.** 클라이언트가 이미 오프셋을 붙인 절대 시각을
    보내므로 서버는 변환할 일이 없고, 이 값은 '사용자가 어느 시계를 보고 골랐는가'를
    남기는 표시용이다. 그래서 여기서는 길이와 글자만 본다 — tzdata가 없는 환경에서
    이름 조회에 실패한다고 예약을 거부하면, 예약이 되고 안 되고가 서버 설치 상태에
    달리게 된다.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError("VALIDATION_FAILED", "timezone must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_TIMEZONE_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"timezone must be at most {MAX_TIMEZONE_CHARS} characters"
        )
    if not all(char.isalnum() or char in "/_+-" for char in text):
        raise BlogTaskError("VALIDATION_FAILED", f"timezone 형식을 읽을 수 없습니다: {text}")
    return text


def validate_reschedule_request(body: Any, *, now: datetime | None = None) -> RescheduleJobRequest:
    """예약 변경 요청(PATCH)의 검증.

    넷 다 선택 항목이지만 **하나도 없으면 거부한다.** 아무것도 바꾸지 않는 요청이
    성공으로 돌아오면, 화면은 바꿨다고 알리는데 실제로는 그대로다.

    두 플랫폼을 **동시에 끄는 것**은 여기서 걸러 낸다 — 부분 변경이라 한쪽만 보낸
    요청은 남은 값을 알아야 판단할 수 있고, 그것은 작업을 들고 있는 서비스가 한다.
    """
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    publish_at = None
    if body.get("publishAt") is not None:
        publish_at = validate_publish_at(body.get("publishAt"), now=now)

    tz = validate_timezone(body.get("timezone"))

    publish_naver = body.get("publishNaver")
    if publish_naver is not None and not isinstance(publish_naver, bool):
        raise BlogTaskError("VALIDATION_FAILED", "publishNaver must be a boolean")

    publish_threads = body.get("publishThreads")
    if publish_threads is not None and not isinstance(publish_threads, bool):
        raise BlogTaskError("VALIDATION_FAILED", "publishThreads must be a boolean")

    if publish_at is None and tz is None and publish_naver is None and publish_threads is None:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            "바꿀 항목(publishAt·timezone·publishNaver·publishThreads)이 없습니다.",
        )
    if publish_naver is False and publish_threads is False:
        raise BlogTaskError(
            "VALIDATION_FAILED", "발행할 플랫폼을 하나 이상 선택해 주세요."
        )
    return RescheduleJobRequest(
        publish_at=publish_at,
        timezone=tz,
        publish_naver=publish_naver,
        publish_threads=publish_threads,
    )


def _validate_schedules(raw: Any, *, now: datetime | None = None) -> list[PlannedPublish]:
    """``schedules``(글별 설정) 배열을 검증한다. 없으면 빈 목록.

    **글과 글 사이는 최소 ``MIN_PUBLISH_GAP_SECONDS``만큼 떨어져야 한다**(2026-08-07).
    예전에는 같은 시각에 여러 편도 허용했다 — "워커가 한 번에 하나씩 올리니 순서대로
    나간다"는 이유였는데, 순서대로 나간다는 말이 곧 **뒤 글은 늦는다**는 뜻이었다.
    지킬 수 없는 약속은 받지 않는다.

    ``publishAt``은 **선택이다.** 있으면 절대 시각 방식, 없으면 간격 방식이고 이 배열은
    '글마다 어디에 올릴지'만 실어 나른다(2026-08-06). 간격 방식도 소재 줄마다 플랫폼을
    고르는데, 그 선택을 담아 보낼 자리가 없어 배치 하나의 값으로 뭉개지고 있었다 —
    화면은 '쓰레드'라고 적어 두고 요약은 '네이버 2건'이라고 말했다.

    **섞어 보내는 것은 거부한다.** 한 배치에서 어떤 글은 시각이 있고 어떤 글은 없으면
    그 배치가 어느 방식인지 정할 수 없다.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BlogTaskError("VALIDATION_FAILED", "schedules must be an array")
    if not raw:
        raise BlogTaskError("VALIDATION_FAILED", "예약할 글을 한 개 이상 넣어 주세요.")
    if len(raw) > MAX_SCHEDULED_TOPICS:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"한 번에 예약할 수 있는 글은 최대 {MAX_SCHEDULED_TOPICS}개입니다.",
        )

    planned: list[PlannedPublish] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BlogTaskError("VALIDATION_FAILED", "each schedule must be an object")
        topic = item.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise BlogTaskError(
                "VALIDATION_FAILED", f"{index + 1}번째 예약의 소재를 입력해 주세요."
            )
        topic = topic.strip()
        if len(topic) > MAX_TOPIC_CHARS:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"각 소재는 {MAX_TOPIC_CHARS}자를 넘을 수 없습니다: {topic[:30]}…",
            )
        publish_naver = item.get("publishNaver")
        if publish_naver is not None and not isinstance(publish_naver, bool):
            raise BlogTaskError("VALIDATION_FAILED", "publishNaver must be a boolean")
        publish_threads = item.get("publishThreads")
        if publish_threads is not None and not isinstance(publish_threads, bool):
            raise BlogTaskError("VALIDATION_FAILED", "publishThreads must be a boolean")
        publish_at = None
        if item.get("publishAt") is not None:
            publish_at = validate_publish_at(
                item.get("publishAt"), now=now, field_name=f"{index + 1}번째 예약의 publishAt"
            )
        # 소재 분야는 **목록 안의 값만** 받는다 — 새 글 작성과 같은 규칙이다
        # (blog_task/validation.py). 자유 문자열을 받으면 프롬프트에 그대로 실려
        # 모델이 뜻을 지어내고, 화면의 12개 버튼과도 어긋난다.
        subject_category = item.get("subjectCategory")
        if subject_category is not None:
            if not isinstance(subject_category, str):
                raise BlogTaskError("VALIDATION_FAILED", "subjectCategory must be a string")
            subject_category = subject_category.strip() or None
        if subject_category is not None and subject_category not in SUBJECT_CATEGORIES:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"subjectCategory must be one of: {', '.join(SUBJECT_CATEGORIES)}",
            )
        planned.append(
            PlannedPublish(
                topic=topic,
                publish_at=publish_at,
                publish_naver=publish_naver,
                publish_threads=publish_threads,
                subject_category=subject_category,
            )
        )

    # **시각이 있는 줄과 없는 줄을 섞어 받는다**(2026-08-12 사용자 요청).
    #
    # 예전에는 "모든 글에 있거나 모든 글에 없어야 한다"고 막았다. 한 배치가 어느 방식인지
    # 정할 수 없다는 이유였는데, 이제 답이 있다 — **시각을 적은 줄은 그 시각에, 안 적은
    # 줄은 앞 글이 끝나면**(after_job_id). 두 규칙이 한 배치 안에서 공존한다.
    #
    # 간격 규칙은 **시각을 적은 줄끼리만** 본다. 안 적은 줄은 약속한 시각이 없어 넘길
    # 시각도 없다 — 앞 글이 끝나야 시작하므로 겹치지도 않는다.
    timed = [item for item in planned if item.publish_at is not None]
    if timed:
        ensure_publish_gap([item.publish_at for item in timed])
    return planned


def _to_datetime(value: str) -> datetime:
    """저장 형식(UTC ISO)을 datetime으로. 여기 오는 값은 이미 검증을 지났다."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gap_message(minutes: int = MIN_PUBLISH_GAP_SECONDS // 60) -> str:
    """간격이 좁을 때의 문구. 화면과 서버가 같은 말을 해야 한다."""
    return (
        f"글과 글 사이 작업 예정 시각은 최소 {minutes}분 이상 떨어져 있어야 합니다. "
        "발행이 한 번에 하나씩 돌기 때문에, 더 촘촘하게 잡으면 뒤 글이 예약 시각을 넘깁니다."
    )


def ensure_publish_gap(publish_ats: list[str]) -> None:
    """예약 시각들이 서로 ``MIN_PUBLISH_GAP_SECONDS`` 이상 떨어져 있는지 본다.

    **입력 순서가 아니라 시각 순으로 본다.** 사용자가 3시·1시·2시 순으로 입력해도
    실제로 올라가는 순서는 1시·2시·3시이고, 간격은 그 순서에서 재야 뜻이 있다.
    """
    ordered = sorted(_to_datetime(value) for value in publish_ats)
    for earlier, later in zip(ordered, ordered[1:]):
        if (later - earlier).total_seconds() < MIN_PUBLISH_GAP_SECONDS:
            raise BlogTaskError("VALIDATION_FAILED", gap_message())


def normalize_topics(raw: Any) -> tuple[list[str], list[str]]:
    """소재 목록을 정리한다. (정리된 목록, 중복이라 버린 것) 을 돌려준다.

    - 앞뒤 공백을 없앤다
    - 빈 줄을 버린다
    - 같은 소재가 반복되면 **첫 번째만** 남긴다
    - 입력 순서를 유지한다
    """
    if not isinstance(raw, list):
        raise BlogTaskError("VALIDATION_FAILED", "topics must be an array")

    cleaned: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise BlogTaskError("VALIDATION_FAILED", "each topic must be a string")
        topic = item.strip()
        if not topic:
            continue
        if len(topic) > MAX_TOPIC_CHARS:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"각 소재는 {MAX_TOPIC_CHARS}자를 넘을 수 없습니다: {topic[:30]}…",
            )
        # 대소문자·내부 공백까지 같아야 중복으로 본다. 사람이 보기에 다른 두 소재를
        # 서버가 임의로 합치면, 화면의 개수와 실제 작업 수가 어긋난다.
        if topic in seen:
            dropped.append(topic)
            continue
        seen.add(topic)
        cleaned.append(topic)
    return cleaned, dropped


def validate_start_batch_request(
    body: Any, *, now: datetime | None = None
) -> StartScheduledBatchRequest:
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    # 글별 절대 발행 시각을 보냈는가. 보냈으면 그것이 예약의 뼈대이고, topics·targetCount는
    # 여기서 파생된다 — 두 벌로 받으면 개수가 어긋날 때 어느 쪽이 사용자의 뜻인지 알 수 없다.
    schedules = _validate_schedules(body.get("schedules"), now=now)

    platform_raw = body.get("platform", SchedulePlatform.NAVER.value)
    if platform_raw != SchedulePlatform.NAVER.value:
        # ``platform``은 배치의 **이름표**이지 게시 대상이 아니다(모델의 SchedulePlatform).
        # 어디에 올릴지는 publishNaver·publishThreads가 정하므로, 이 자리에 다른 값을
        # 넣은 요청은 서로 다른 두 곳에서 채널을 정하려는 것이다 — 조용히 고치지 않고 막는다.
        raise BlogTaskError(
            "VALIDATION_FAILED",
            "platform은 naver여야 합니다. 발행할 곳은 publishNaver·publishThreads로 "
            "선택해 주세요.",
        )

    # 게시 대상 두 스위치. 옛 클라이언트는 publishNaver를 보내지 않으므로 기본은 True다 —
    # 그때는 네이버가 언제나 발행 대상이었고, False로 읽으면 예약이 아무 데도 안 올라간다.
    publish_naver = body.get("publishNaver", True)
    if not isinstance(publish_naver, bool):
        raise BlogTaskError("VALIDATION_FAILED", "publishNaver must be a boolean")

    publish_threads = body.get("publishThreads", False)
    if not isinstance(publish_threads, bool):
        raise BlogTaskError("VALIDATION_FAILED", "publishThreads must be a boolean")

    mode_raw = body.get("topicMode", ScheduleTopicMode.MULTI.value)
    if mode_raw not in (ScheduleTopicMode.MULTI.value, ScheduleTopicMode.SINGLE.value):
        raise BlogTaskError("VALIDATION_FAILED", "topicMode must be multi or single")
    topic_mode = ScheduleTopicMode(mode_raw)

    # schedules가 오면 그것이 곧 작업 목록이다. 같은 소재를 서로 다른 시각에 두 번
    # 예약하는 것도 사용자가 실제로 하는 일이라, 여기서는 중복을 걸러 내지 않는다
    # (topics만 온 옛 요청의 normalize_topics와 다른 점이다).
    if schedules:
        # 글마다 고른 플랫폼이 실제로 하나 이상인지 여기서 본다. 글별 값이 없으면
        # 배치 기본값을 물려받으므로, 둘을 합친 **결과**로 판단해야 한다.
        for index, item in enumerate(schedules):
            naver = publish_naver if item.publish_naver is None else item.publish_naver
            threads = publish_threads if item.publish_threads is None else item.publish_threads
            if not naver and not threads:
                raise BlogTaskError(
                    "VALIDATION_FAILED",
                    f"{index + 1}번째 예약의 발행 플랫폼을 하나 이상 선택해 주세요.",
                )
        # **한 줄이라도 시각이 있으면 절대 시각 방식이다.** 섞인 배치에서 시각을 적은
        # 줄의 약속을 지키려면 워커가 시각을 보는 쪽(advance_scheduled_batch)으로 돌아야
        # 한다. 시각이 없는 줄은 그 안에서 앞 글을 기다린다(after_job_id).
        timed = any(item.publish_at is not None for item in schedules)
        return StartScheduledBatchRequest(
            topics=[item.topic for item in schedules],
            target_count=len(schedules),
            interval_seconds=_validated_interval(body),
            platform=SchedulePlatform.NAVER,
            topic_mode=topic_mode,
            publish_naver=publish_naver,
            publish_threads=publish_threads,
            client_request_id=_validated_client_request_id(body),
            schedule_mode=ScheduleMode.ABSOLUTE if timed else ScheduleMode.INTERVAL,
            schedules=schedules,
            timezone=validate_timezone(body.get("timezone")),
            brand_id=_validated_brand_id(body),
        )

    # 간격 방식은 배치 하나의 값이 모든 글에 적용된다. 둘 다 꺼져 있으면 아무 데도
    # 올라가지 않는 예약이 만들어진다.
    if not publish_naver and not publish_threads:
        raise BlogTaskError("VALIDATION_FAILED", "발행할 플랫폼을 하나 이상 선택해 주세요.")

    topics, dropped = normalize_topics(body.get("topics"))
    if not topics:
        raise BlogTaskError("VALIDATION_FAILED", "예약할 소재를 한 개 이상 입력해 주세요.")
    if len(topics) > MAX_SCHEDULED_TOPICS:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"한 번에 예약할 수 있는 소재는 최대 {MAX_SCHEDULED_TOPICS}개입니다.",
        )
    if topic_mode is ScheduleTopicMode.SINGLE:
        # 소재 하나로 여러 편을 쓰는 모드다. 여러 줄이 들어와도 첫 줄만 쓴다 —
        # 나머지를 조용히 섞으면 사용자가 고른 모드와 다른 글이 나온다.
        topics = topics[:1]

    target_count = body.get("targetCount", len(topics))
    # bool은 int의 하위형이라 isinstance만으로는 True가 1로 통과한다.
    if isinstance(target_count, bool) or not isinstance(target_count, int):
        raise BlogTaskError("VALIDATION_FAILED", "targetCount must be an integer")
    if target_count < 1:
        raise BlogTaskError("VALIDATION_FAILED", "글의 개수는 1개 이상이어야 합니다.")
    if topic_mode is ScheduleTopicMode.SINGLE:
        # 소재는 하나뿐이므로 글의 개수는 소재 수에 매이지 않는다. 상한만 지킨다.
        if target_count > MAX_SCHEDULED_TOPICS:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"한 번에 예약할 수 있는 글은 최대 {MAX_SCHEDULED_TOPICS}개입니다.",
            )
    elif target_count > len(topics):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"글의 개수({target_count}개)가 입력한 소재 수({len(topics)}개)보다 많습니다.",
        )

    # 소재별 한 편 모드에서 글의 개수가 소재 수보다 적으면 **앞에서부터** 그만큼만
    # 예약한다. 입력 순서가 곧 사용자가 정한 우선순위다. 소재 하나 모드에서는 소재가
    # 하나뿐이므로 자르지 않는다.
    return StartScheduledBatchRequest(
        topics=topics if topic_mode is ScheduleTopicMode.SINGLE else topics[:target_count],
        target_count=target_count,
        interval_seconds=_validated_interval(body),
        platform=SchedulePlatform.NAVER,
        topic_mode=topic_mode,
        publish_naver=publish_naver,
        publish_threads=publish_threads,
        client_request_id=_validated_client_request_id(body),
        dropped_duplicates=dropped,
        timezone=validate_timezone(body.get("timezone")),
        brand_id=_validated_brand_id(body),
    )


def _validated_interval(body: dict) -> int:
    """생성 작업 간격(초). 옛 클라이언트는 intervalMinutes를 보낸다 — 둘 다 없으면 오류다.

    절대 시각 방식에서도 이 값은 필요하다. 발행 시각과는 별개로, 원고 생성을 몇 개씩
    한꺼번에 돌리지 않기 위한 간격이기 때문이다.
    """
    interval = body.get("intervalSeconds")
    if interval is None:
        minutes = body.get("intervalMinutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            interval = minutes * 60
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise BlogTaskError("VALIDATION_FAILED", "intervalSeconds must be an integer")
    if interval < MIN_INTERVAL_SECONDS or interval > MAX_INTERVAL_SECONDS:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"발행 간격은 {MIN_INTERVAL_SECONDS}초 이상 "
            f"{MAX_INTERVAL_SECONDS // 60}분 이하여야 합니다.",
        )
    return interval


def _validated_brand_id(body: dict) -> str | None:
    """이 배치의 글에 활용할 브랜드(2026-08-19). 선택이다.

    **여기서는 있는 브랜드인지 확인하지 않는다.** 그것은 남의 브랜드를 가리키는지까지
    봐야 하는 일이라 사용자를 아는 자리(서비스)에서 한다 — 검증기는 요청 본문의 모양만
    본다. 없는 브랜드를 걸면 첫 글을 만들 때 그 작업이 실패한다.
    """
    value = body.get("brandId")
    if value is None:
        return None
    if not isinstance(value, str):
        raise BlogTaskError("VALIDATION_FAILED", "brandId must be a string")
    return value.strip() or None


def _validated_client_request_id(body: dict) -> str | None:
    client_request_id = body.get("clientRequestId")
    if client_request_id is None:
        return None
    if not isinstance(client_request_id, str) or not client_request_id.strip():
        raise BlogTaskError(
            "VALIDATION_FAILED", "clientRequestId must be a non-empty string when provided"
        )
    client_request_id = client_request_id.strip()
    if len(client_request_id) > MAX_CLIENT_REQUEST_ID_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"clientRequestId must be at most {MAX_CLIENT_REQUEST_ID_CHARS} characters",
        )
    return client_request_id
