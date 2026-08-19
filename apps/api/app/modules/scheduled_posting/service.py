"""예약 포스팅 오케스트레이션.

**이 모듈은 글을 쓰지 않는다.** 기존 새 글 작성이 쓰는 서비스들을 순서대로 부를 뿐이다:

    create_blog_task → recommend_topics → generate_topics → select_topic
    → analyze_intent_candidates → select_intent → generate_draft → publish_blog_task

원고·이미지 품질에 관한 것은 하나도 여기 없다. 프롬프트·이미지 정책·발행 계획은 전부
호출되는 쪽에 그대로 있고, 이 파일은 '어느 것을 언제 부르는가'만 안다.

읽기 전에 알아야 할 함정 넷(전부 실제 코드에서 확인했다):

1. ``generate_draft``는 **실패해도 예외를 던지지 않는다**. 실패는
   status로만 드러나므로, try/except로 성공을 판정하면 전부 성공으로 읽힌다.
2. ``analyze_intent_candidates``는 선언이 ``-> BlogTask``지만 **None을 돌려줄 수 있다**
   (저장소 조회 결과를 그대로 반환한다).
3. M3 검증 실패는 status를 바꾸지 않는다 — 글은 SEARCH_ANALYZING에 남고
   ``intent_validation_result.provider == "none"``만이 실패 신호다.
4. 낙관적 잠금 충돌이 실제 상태 위반과 **같은 에러 코드**를 쓴다(repository.py의
   "was updated concurrently"). 문구로 구분해 한 번 다시 읽는다.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.errors import BlogTaskError
# 글에 브랜드 자료를 얹는 일은 브랜드 모듈이 한 곳에서 한다 — 화면에서 만드는 글
# (`POST /posts`)과 여기서 만드는 글의 브랜드 처리가 갈라지면 안 된다(2026-08-19).
from app.modules.brand import with_brand_materials
from app.shared import BlogTaskStatus, PostingMethod, PostingResultStatus
from app.shared.format import now_iso
from app.shared.ids import new_batch_id, new_job_id, new_series_id, short

from .models import (
    ACTIVE_BATCH_STATUSES,
    RESCHEDULABLE_JOB_STATUSES,
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledBatchView,
    ScheduledJob,
    ScheduledJobListItem,
    ScheduledJobStage,
    ScheduledJobStatus,
    ScheduledLogEntry,
    ScheduleMode,
    SchedulePlatform,
    ScheduleTopicMode,
    has_appointment,
    publishes_anywhere,
)
from .repository import ScheduledPostingRepository
from .validation import (
    SCHEDULED_DEFAULT_PURPOSE,
    ensure_publish_gap,
    validate_reschedule_request,
    validate_start_batch_request,
)

logger = logging.getLogger(__name__)

#: 소재 관련 키워드를 몇 개까지 받아 볼지. 첫 번째만 쓰지만, 자격 미달 키워드가 섞여
#: 오므로 고를 여지를 남긴다.
TREND_MAX_KEYWORDS = 8

#: 원고 생성 전 단계(M1~M3)에서 일시적 오류가 났을 때의 자동 재시도 횟수와 대기.
#: 발행 단계에는 적용하지 않는다 — 중복 게시 위험이 재시도 이득보다 크다.
PRE_DRAFT_RETRY_LIMIT = 1
PRE_DRAFT_RETRY_DELAY_SECONDS = 5.0

#: M3 검증이 완전히 실패했을 때 provider에 적히는 값.
INTENT_PROVIDER_FAILED = "none"

#: 새 글 작성에서 넘어온 예약이 만드는 배치의 기본 간격(초).
#:
#: **절대 시각 배치에서는 쓰이지 않는다** — 이 값을 읽는 곳은 간격(INTERVAL) 방식뿐이다
#: (worker._seconds_until_next 주석). 모델이 요구하는 값이라 자리만 채운다.
#:
#: 예전 이름은 ``PREPARE_LEAD_SECONDS``였고 "발행 시각보다 얼마나 일찍 원고를 만들기
#: 시작하는가"라는 뜻이었다. 그 여유는 2026-08-12에 없앴다(사용자 지시: "그 20분 차이
#: 굳이 없어도 되는거잖아"). 지금은 저장된 시각이 곧 작업을 시작할 시각이다.
DEFAULT_BATCH_INTERVAL_SECONDS = 20 * 60

#: 발행이 실패했을 때 자동으로 다시 시도하는 횟수와 그 사이의 대기.
#:
#: **발행기가 '실패'라고 분명히 말한 경우에만** 다시 시도한다. 결과를 알 수 없는 실패
#: (발행 도중 서버가 죽은 경우)는 recovery.py가 사람 확인으로 돌린다 — 그것까지 자동으로
#: 다시 올리면 같은 글이 두 번 게시된다.
MAX_PUBLISH_ATTEMPTS = 3
PUBLISH_RETRY_BACKOFF_SECONDS = 300


class ScheduledPostingError(BlogTaskError):
    """예약 포스팅 고유의 실패. 에러 코드가 화면 분기에 쓰인다."""


class _Stopped(Exception):
    """사용자가 정지를 눌러 더 진행하지 않는다."""


class _Paused(Exception):
    """사용자가 일시정지를 눌러 안전한 지점에서 멈춘다."""


class _Canceled(Exception):
    """사용자가 이 예약을 취소했다 — 상태를 CANCELED로 둔 채 멈춘다."""


def _parse_iso(value: str | None):
    """ISO 문자열을 datetime으로. 못 읽으면 None(예고를 생략할 뿐, 실패가 아니다)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _after_seconds(seconds: float) -> str:
    """지금부터 ``seconds`` 뒤의 UTC ISO 문자열. 저장되는 모든 시각의 형식이 같아야 한다."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_iso(value: datetime) -> str:
    """datetime을 저장 형식(UTC, 밀리초, Z)으로 — _after_seconds와 같은 모양이어야 한다."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _when_label(publish_at: str | None) -> str:
    """로그에 적을 시각 표현. 이 PC의 지역 시간으로 적는다 — 로그를 읽는 사람의 시계다."""
    due = _parse_iso(publish_at)
    if due is None:
        return "곧"
    return f"{due.astimezone():%m월 %d일 %H시 %M분}에"


def _publish_at_for(scheduled_run_at: str) -> str:
    """사용자가 고른 **원고 작업 시작 시각**을 그대로 쓴다(2026-08-12).

    예전에는 여기서 준비 여유(20분)를 더했다. 그때는 입력이 **발행 시각**이었기 때문이다 —
    그 시각에 올리려면 미리 만들어야 하니 워커가 ``publish_at - 여유``에 준비를 시작했다.

    입력이 작업 시작 시각으로 바뀌면서 그 계산은 **더하고 빼서 제자리**가 됐다
    (사용자 지적: "준비 여유 이런거 굳이 있어야해? 난 필요 없다고 생각하는데").
    지금은 값을 그대로 두고, 이 작업의 준비 문지기도 여유 없이 이 시각을 본다.

    형식만 UTC로 맞춘다 — 시간대 표기가 없으면 UTC로 읽는다(서버는 UTC 한 가지로 잰다).
    """
    parsed = datetime.fromisoformat(scheduled_run_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _naver_saved(user_id: str) -> bool:
    """네이버 저장 정보가 있는지. GET /naver/status와 **같은 방법**으로 본다.

    비밀번호는 읽지 않는다. 저장 여부만 확인하고, 실제 로그인은 발행 시점에 기존
    ConnectedNaverPublisher가 한다.
    """
    from app.posting.config import naver_profile_dir
    from app.posting.credentials import saved_username

    return saved_username(naver_profile_dir(user_id)) is not None


def _channels_for(request: Any, item: Any = None) -> tuple[bool, bool]:
    """이 글이 올라갈 곳 — ``(네이버, 쓰레드)``.

    글별 값(``schedules``의 한 줄)이 있으면 그것이 이기고, 없으면(None) 배치 기본값을
    물려받는다. ``item``을 주지 않으면 배치 기본값 그대로다(간격 방식).

    **둘 다 False인 조합은 여기 오지 않는다** — 검증(validation.py)이 먼저 거부한다.
    """
    if item is None:
        return request.publish_naver, request.publish_threads
    return (
        request.publish_naver if item.publish_naver is None else item.publish_naver,
        request.publish_threads if item.publish_threads is None else item.publish_threads,
    )


def _threads_saved(user_id: str) -> bool:
    """스레드 저장 정보가 있는지. GET /threads/status와 같은 방법으로 본다."""
    from app.posting.credentials import saved_username
    from app.posting.threads_browser import threads_profile_dir

    return saved_username(threads_profile_dir(user_id)) is not None


def _is_concurrent_update(error: BlogTaskError) -> bool:
    """낙관적 잠금 충돌인가. 실제 상태 위반과 코드가 같아 문구로 가른다."""
    return "concurrently" in (error.message or "")


def _sum_relevance(candidate: Any) -> float:
    total = 0.0
    for source in getattr(candidate, "sources", []) or []:
        score = getattr(source, "relevance_score", None)
        if isinstance(score, (int, float)):
            total += float(score)
    return total


def pick_intent(candidates: list[Any]) -> Any | None:
    """자동 의도 선택.

    1) sources의 relevanceScore 합계가 가장 높은 후보
    2) 동점이면 sources 개수가 많은 후보
    3) 다시 동점이면 원래 반환 순서가 빠른 후보

    새 LLM 호출은 하지 않는다 — 검증이 이미 매긴 점수만 읽는다.
    """
    if not candidates:
        return None
    best_index = 0
    best_key = (
        -_sum_relevance(candidates[0]),
        -len(getattr(candidates[0], "sources", []) or []),
        0,
    )
    for index, candidate in enumerate(candidates[1:], start=1):
        key = (
            -_sum_relevance(candidate),
            -len(getattr(candidate, "sources", []) or []),
            index,
        )
        if key < best_key:
            best_key = key
            best_index = index
    return candidates[best_index]


def pick_title(candidates: list[Any]) -> Any | None:
    """자동 제목 선택. 새 제목을 만들지 않고 기존 후보 중에서만 고른다.

    1) recommended가 True인 후보
    2) 없으면 score가 가장 높은 후보
    3) score가 모두 없으면 첫 번째 후보
    """
    if not candidates:
        return None
    for candidate in candidates:
        if getattr(candidate, "recommended", False) is True:
            return candidate
    scored = [c for c in candidates if isinstance(getattr(c, "score", None), (int, float))]
    if scored:
        return max(scored, key=lambda c: float(c.score))
    return candidates[0]


def pick_trend_keyword(keywords: list[Any], variant: int = 0) -> Any | None:
    """서버가 준 순서를 존중해, 자격을 갖춘 키워드를 고른다.

    ``isEligible`` 필드가 아예 없는 응답은 전부 자격이 있는 것으로 본다 — 없는 필드를
    False로 읽으면 키워드가 있는데도 매번 트렌드를 건너뛰게 된다.

    ``variant``는 같은 소재로 여러 편을 쓸 때 **몇 번째 글인가**다. 같은 키워드로 N번
    쓰면 N편이 서로 닮으므로, 변종마다 다음 키워드를 집어 각도를 벌린다. 키워드가
    변종 수보다 적으면 돌려 쓴다(그때는 제목 배제가 차이를 만든다).
    """
    eligible = [
        keyword
        for keyword in keywords
        if getattr(keyword, "is_eligible", None) in (None, True)
    ]
    if not eligible:
        return None
    return eligible[variant % len(eligible)]


def _scheduled_message(topic: str, count: int) -> str:
    """예약을 걸었을 때의 안내. 여러 편이면 **줄지어 돈다**는 사실을 함께 말한다."""
    if count <= 1:
        return f"'{topic}' 글을 예약했습니다. 그 시각에 자료를 새로 모아 원고를 만듭니다."
    return (
        f"'{topic}' 글 {count}편을 예약했습니다. 첫 편을 그 시각에 시작하고, "
        "한 편이 끝나면 다음 편이 이어서 돕니다."
    )


def _added_message(topic: str, count: int) -> str:
    if count <= 1:
        return f"'{topic}' 글을 예약에 추가했습니다. 원고는 그 시각에 만듭니다."
    return (
        f"'{topic}' 글 {count}편을 예약에 추가했습니다. 한 편이 끝나면 다음 편이 "
        "이어서 돕니다."
    )


class ScheduledPostingService:
    def __init__(
        self,
        repository: ScheduledPostingRepository,
        blog_task_service: Any,
        trend_service: Any,
        draft_service: Any,
        brand_service: Any = None,
    ):
        self._repository = repository
        self._blog_tasks = blog_task_service
        self._trends = trend_service
        self._drafts = draft_service
        # 브랜드 자료를 글에 얹기 위한 것(2026-08-19). **선택이다** — 주지 않으면 예전
        # 그대로 브랜드 없는 글을 만든다. 브랜드를 건 배치에서만 필요하고, 브랜드 없이
        # 이 서비스를 세우는 자리(테스트·부분 구성)를 깨지 않으려는 것이다.
        self._brands = brand_service

    # ------------------------------------------------------------------ 조회

    async def _view(self, batch: ScheduledBatch) -> ScheduledBatchView:
        jobs = await self._repository.list_jobs(batch.batch_id)
        return ScheduledBatchView(batch=batch, jobs=jobs)

    # ------------------------------------------------------------------ 집계

    async def _counts_of(self, batch_id: str) -> dict[str, int]:
        """완료·실패·취소 개수를 **실제 작업에서 다시 센다.**

        하나씩 더하는 방식은 언젠가 어긋난다. 실제로 어긋났다(2026-08-06 신고):

        - ``retry_job``은 실패를 다시 대기로 되돌리면서 ``failed_count``를 줄이지
          않았다. 그 작업이 나중에 성공하면 같은 작업이 실패 1 · 완료 1로 두 번 세어진다.
        - ``_fail``은 부를 때마다 1씩 더했다. 한 작업이 두 번 실패하면 2가 된다.

        저장된 배치에서 그대로 확인된다: 작업 2건짜리 배치가 ``done=1 fail=2``였다.
        화면의 진행률은 (완료+실패+취소)/전체라, 그 배치가 **100%**로 보였다 —
        발행된 글은 하나뿐인데 "다 됐다"로 읽힌다.

        삭제 경로는 이미 이렇게 다시 세고 있었다(``delete_job``). 같은 규칙을 성공·
        실패·취소·재시도에도 쓴다.
        """
        jobs = await self._repository.list_jobs(batch_id)
        return {
            "completed_count": sum(
                1 for job in jobs if job.status == ScheduledJobStatus.COMPLETED
            ),
            "failed_count": sum(
                1 for job in jobs if job.status == ScheduledJobStatus.FAILED
            ),
            "canceled_count": sum(
                1 for job in jobs if job.status == ScheduledJobStatus.CANCELED
            ),
        }

    async def get_active_batch(self, user_id: str) -> ScheduledBatchView | None:
        batch = await self._repository.find_active_batch(user_id)
        return await self._view(batch) if batch else None

    async def get_batch(self, user_id: str, batch_id: str) -> ScheduledBatchView:
        batch = await self._repository.find_user_batch(user_id, batch_id)
        if batch is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 배치 {batch_id}를 찾을 수 없습니다.")
        return await self._view(batch)

    # ------------------------------------------------------------------ 시작

    async def start_batch(self, user_id: str, raw_body: Any) -> ScheduledBatchView:
        request = validate_start_batch_request(raw_body)

        # 같은 클릭이 두 번 도착했으면 배치를 하나 더 만들지 않고 있던 것을 돌려준다.
        if request.client_request_id:
            existing = await self._repository.find_batch_by_client_request(
                user_id, request.client_request_id
            )
            if existing is not None:
                return await self._view(existing)

        # 남아 있는 배치가 있으면 **정지를 따로 누르지 않아도** 새 입력값으로 갈아탄다.
        # 다만 지금 글을 쓰거나 발행하는 중이면 손대지 않는다 — 도는 중인 LLM·Selenium을
        # 버리면 네이버에 올라갔는지 알 수 없는 글이 생기고, 그게 중복 게시의 씨앗이다.
        active = await self._repository.find_active_batch(user_id)
        if active is not None:
            if await self._is_executing(active):
                raise ScheduledPostingError(
                    "VALIDATION_FAILED",
                    "지금 글을 쓰거나 발행하는 중입니다. 그 작업이 끝난 뒤 다시 시작해 주세요.",
                )
            await self._supersede(active)

        # 게시 대상은 배치 기본값으로도, **글별로도** 고를 수 있다. 한 곳이라도 쓰는
        # 플랫폼만 연결을 확인한다 — 쓰레드에만 올리는 예약을 네이버 계정이 없다고
        # 막으면, 사용자가 고른 대로 발행할 수 없다(2026-08-06).
        #
        # 확인은 **시작 시점에** 한다. 여기서 안 막으면 그 작업만 발행 단계에서 죽고,
        # 사용자는 몇 분 뒤에야 이유를 알게 된다.
        wanted = (
            [_channels_for(request, item) for item in request.schedules]
            if request.schedules
            else [_channels_for(request)]
        )
        wants_naver = any(naver for naver, _ in wanted)
        wants_threads = any(threads for _, threads in wanted)
        if wants_naver and not _naver_saved(user_id):
            raise ScheduledPostingError(
                "NAVER_NOT_CONNECTED", "설정에서 Naver 계정을 먼저 저장해 주세요."
            )
        if wants_threads and not _threads_saved(user_id):
            raise ScheduledPostingError(
                "THREADS_NOT_CONNECTED", "설정에서 Threads 계정을 먼저 저장해 주세요."
            )

        now = now_iso()
        batch_id = new_batch_id()

        absolute = request.schedule_mode is ScheduleMode.ABSOLUTE
        # **시각을 적지 않은 줄이 섞여 있는가**(2026-08-12). 섞였으면 순서를 시각으로 세울
        # 수 없다 — 시각 없는 줄은 '앞 글이 끝나면'이 곧 시각이라, 재정렬하면 그 '앞'이
        # 사용자가 적은 줄과 달라진다. 그래서 섞인 배치는 **입력한 줄 순서**를 지킨다.
        mixed = absolute and any(item.publish_at is None for item in request.schedules)
        if absolute:
            # 글별 절대 시각 방식. 순서는 **발행 시각 순**이다 — 사용자가 입력한 줄
            # 순서가 아니라 실제로 올라갈 순서로 세워야, 워커가 sequence대로 집어도
            # 시각을 거스르지 않는다. (섞인 배치만 예외로 줄 순서를 지킨다.)
            ordered = (
                list(request.schedules)
                if mixed
                else sorted(
                    request.schedules, key=lambda item: (item.publish_at or "", item.topic)
                )
            )
            seen_topics: dict[str, int] = {}
            planned = []
            for item in ordered:
                # 같은 소재를 여러 시각에 예약하면 서로 다른 각도로 쓰게 변종 번호를 매긴다
                # (소재 하나 모드와 같은 장치다).
                variant = seen_topics.get(item.topic, 0)
                seen_topics[item.topic] = variant + 1
                naver, threads = _channels_for(request, item)
                planned.append(
                    (item.topic, variant, item.publish_at, naver, threads, item.subject_category)
                )
        elif request.schedules:
            # 간격 방식인데 글별 설정이 왔다 — 소재 줄마다 고른 플랫폼을 담아 온 것이다
            # (2026-08-06). 발행 시각은 없으므로 순서는 **입력한 줄 순서** 그대로다.
            seen_topics = {}
            planned = []
            for item in request.schedules:
                variant = seen_topics.get(item.topic, 0)
                seen_topics[item.topic] = variant + 1
                naver, threads = _channels_for(request, item)
                planned.append(
                    (item.topic, variant, None, naver, threads, item.subject_category)
                )
        elif request.topic_mode is ScheduleTopicMode.SINGLE:
            # 소재 하나로 여러 편: 같은 소재를 target_count번 되풀이하되 변종 번호를 매긴다.
            # 소재 분야는 글별 설정(schedules)으로만 오므로 여기서는 없다.
            planned = [
                (request.topics[0], variant, None, *_channels_for(request), None)
                for variant in range(request.target_count)
            ]
        else:
            # 소재별 한 편: 입력한 소재가 그대로 목록이다.
            planned = [
                (topic, 0, None, *_channels_for(request), None) for topic in request.topics
            ]

        # id를 미리 뽑아 둔다 — 시각을 적지 않은 줄이 **앞 줄을 가리켜야** 하기 때문이다.
        job_ids = [new_job_id() for _ in planned]
        # 한 번에 건 묶음. **소재마다 하나**다(2026-08-13) — 한 소재로 여러 편을 거는
        # SINGLE 모드가 한 묶음이고, 소재별 한 편인 MULTI 모드는 소재마다 한 줄이라
        # 어차피 편수 표시가 붙지 않는다.
        series_of: dict[str, str] = {}
        for topic, *_ in planned:
            series_of.setdefault(topic, new_series_id())

        def after_of(index: int) -> str | None:
            """이 작업보다 먼저 끝나야 하는 작업. 시각을 적지 않은 줄에만 있다.

            시각이 없다는 것은 '앞 글이 발행되면 이어서'라는 뜻이다(2026-08-12 사용자
            결정). 그 뜻을 담는 자리가 이미 있다 — 한 소재로 여러 편을 만들 때 쓰는
            ``after_job_id``다. 새 규칙을 만들지 않고 그것을 그대로 쓴다.

            **첫 줄은 가리킬 앞이 없다** — 시각이 없으면 시작하자마자 돈다.
            섞이지 않은 배치(전부 시각 있음 / 전부 없음)는 예전 그대로 None이다.
            """
            if not mixed or index == 0:
                return None
            if planned[index][2] is not None:
                # 시각을 적은 줄은 그 시각이 곧 약속이다. 앞 글에 매달면 앞이 늦어질 때
                # 약속을 놓친다.
                return None
            return job_ids[index - 1]

        jobs = [
            ScheduledJob(
                job_id=job_ids[index],
                batch_id=batch_id,
                user_id=user_id,
                platform=SchedulePlatform.NAVER,
                sequence=index,
                topic=topic,
                series_id=series_of[topic],
                variant_index=variant,
                subject_category=category,
                # 브랜드는 **배치 전체에 하나**다. 값은 작업이 들고 있는다 — 글을 만드는
                # 것은 작업이고, 배치에서 찾아 쓰면 재시도마다 배치를 다시 읽어야 한다.
                brand_id=request.brand_id,
                publish_naver=naver,
                publish_threads=threads,
                publish_at=publish_at,
                after_job_id=after_of(index),
                timezone=request.timezone,
                # 첫 작업은 즉시다. 나머지 시각은 앞 글이 실제로 발행된 뒤에 정해진다 —
                # 원고 생성 시간이 글마다 다르므로 지금 계산해 두면 전부 어긋난다.
                # (절대 시각 방식에서는 이 값이 아니라 publish_at이 발행 시점을 정한다.)
                scheduled_at=now if index == 0 and not absolute else None,
                created_at=now,
                updated_at=now,
            )
            for index, (topic, variant, publish_at, naver, threads, category) in enumerate(
                planned
            )
        ]
        batch = ScheduledBatch(
            batch_id=batch_id,
            user_id=user_id,
            platform=SchedulePlatform.NAVER,
            topic_mode=request.topic_mode,
            schedule_mode=request.schedule_mode,
            timezone=request.timezone,
            publish_naver=request.publish_naver,
            publish_threads=request.publish_threads,
            brand_id=request.brand_id,
            status=ScheduledBatchStatus.READY,
            target_count=request.target_count,
            interval_seconds=request.interval_seconds,
            total_count=len(jobs),
            client_request_id=request.client_request_id,
            created_at=now,
            updated_at=now,
            logs=[
                ScheduledLogEntry(
                    at=now,
                    message=(
                        f"예약 작업 {len(jobs)}건이 생성되었습니다."
                        if not absolute
                        else f"예약 발행 {len(jobs)}건이 등록되었습니다."
                    ),
                    tone="info",
                )
            ],
        )
        await self._repository.create_batch(batch, jobs)
        if request.dropped_duplicates:
            await self._log(
                batch_id,
                f"중복된 소재 {len(request.dropped_duplicates)}건은 제외했습니다.",
                tone="muted",
            )
        return await self._view(batch)

    # ------------------------------------------------- 새 글 작성에서 넘어온 예약

    async def schedule_prepared_post(
        self,
        user_id: str,
        post_id: str,
        # 짝 하나는 {"intentId": str, "title": str?, "intent": IntentCandidate?}다.
        additional_drafts: Sequence[Mapping[str, Any]] = (),
        primary_draft: Mapping[str, Any] | None = None,
    ) -> ScheduledBatchView:
        """새 글 작성에서 **방향까지 고른 글**을 예약 작업으로 넘겨받는다(2026-08-11).

        단일 글 작성은 그대로다 — 소재 단계에서 작업 시각을 비워 두면 이 경로를 지나지
        않고 예전처럼 곧바로 원고를 만든다. 시각을 넣었을 때만 여기로 온다.

        넘겨받는 것은 **글 하나를 가리키는 작업 하나**다. 설정을 예약 문서로 복사하지
        않는다 — 소재·키워드·목적·트렌드·제목·고른 방향은 전부 그 BlogTask에 이미 있고,
        복사하면 두 벌이 어긋난다(ScheduledJob.post_id 주석의 원칙 그대로).

        저장하는 시각은 **사용자가 고른 작업 시각 그대로**다. 워커도 그 시각에 준비를
        시작한다 — 더하고 빼는 '준비 여유'는 2026-08-12에 없앴다.

        이미 절대 시각 예약이 돌고 있으면 **그 배치에 한 줄로 붙인다.** 새 배치를 만들면
        기존 예약을 밀어내게 되고(start_batch의 supersede), 사용자가 걸어 둔 다른 글들이
        사라진다. 간격 방식 예약이 돌고 있으면 시각 개념이 서로 달라 붙일 수 없으므로
        그렇다고 알린다.
        """
        task = await self._blog_tasks.get_user_blog_task(user_id, post_id)
        if task is None:
            raise ScheduledPostingError("NOT_FOUND", "글을 찾을 수 없습니다.")
        # 시각을 비웠으면 **지금**이다(2026-08-12). 한 소재로 여러 편을 만들 때는 시각을
        # 지정하지 않아도 작업 큐로 넘어가야 한다 — 한 화면에서 여러 편을 만들 길이 없기
        # 때문이다. 한 편짜리는 예전처럼 이 경로를 아예 지나지 않는다(화면이 안 부른다).
        chosen_run_at = (task.input.scheduled_run_at or "").strip()
        # 시각을 고르지 않은 작업은 '지금 바로'다. 그 사실을 작업에 적어 둔다(2026-08-13) —
        # publish_at에는 어차피 지금이 들어가므로, 값만 봐서는 사용자가 정한 약속인지
        # 그냥 지금인지 가릴 수 없다. 발행 순서가 그 구분을 본다.
        starts_immediately = not chosen_run_at
        run_at = chosen_run_at or now_iso()
        # 방향(selectedIntent)을 요구하지 않는다(2026-08-11). 방향 후보를 만들려면 자료를
        # 모아야 하는데, 예약 글은 그 수집을 **작업 시각으로 미뤄 두었다** — 며칠 뒤에 쓸
        # 글의 자료를 오늘 모으면 그 사이에 나온 이슈가 빠지기 때문이다. 그래서 예약 글은
        # 방향 없이 넘어오고, 작업 시각에 검증과 방향 선택이 함께 돈다.
        if task.trend_selection is None:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", "제목을 먼저 확정해 주세요."
            )
        if not _naver_saved(user_id):
            raise ScheduledPostingError(
                "NAVER_NOT_CONNECTED", "설정에서 Naver 계정을 먼저 저장해 주세요."
            )

        existing = await self._repository.list_user_jobs(user_id, limit=200)
        already = next(
            (job for job in existing if job.post_id == post_id and job.status in RESCHEDULABLE_JOB_STATUSES),
            None,
        )
        publish_at = _publish_at_for(run_at)
        if already is not None:
            # 같은 글을 두 번 예약하면 같은 원고가 두 번 올라간다. 시각만 고쳐 준다.
            view = await self.reschedule_job(
                user_id, already.job_id, {"publishAt": publish_at}
            )
            if not starts_immediately:
                return view
            # '지금 바로'로 다시 건 것이면 그 표시를 되살린다 — reschedule는 시각이 실린
            # 요청을 '사용자가 약속을 정했다'로 읽고 표시를 지운다.
            refreshed = await self._repository.find_user_job(user_id, already.job_id)
            if refreshed is not None:
                await self._repository.save_job(
                    refreshed.model_copy(update={"starts_immediately": True})
                )
            return await self._view(await self._require_batch(user_id, already.batch_id))

        now = now_iso()
        active = await self._repository.find_active_batch(user_id)
        if active is not None and active.schedule_mode is not ScheduleMode.ABSOLUTE:
            raise ScheduledPostingError(
                "VALIDATION_FAILED",
                "간격 방식 예약이 돌고 있어 시각 예약을 붙일 수 없습니다. "
                "그 예약을 정지한 뒤 다시 시도해 주세요.",
            )

        batch_id = active.batch_id if active is not None else new_batch_id()

        # 편마다 자기 방향을 가진 글이 하나씩 있어야 한다(2026-08-12). 원본이 1편이고,
        # 나머지는 방향만 다른 복제다. 복제가 실패하면 **거기서 멈추고 만든 것까지만**
        # 건다 — 이미 만든 글을 되돌리는 것보다, 몇 편이 걸렸는지 알려 주는 편이 낫다.
        post_ids = [post_id]
        # 첫 편은 **원본 글**이다(2026-08-12). 화면이 라운드마다 고른 것을 마지막에 한 번에
        # 보내므로, 그 첫 짝을 여기서 원본에 적용한다 — 라운드마다 저장하면 글 하나의 한
        # 자리를 두고 서로 덮어쓴다.
        if primary_draft is not None:
            task = await self._blog_tasks.apply_round_pick(
                post_id,
                primary_draft["intentId"],
                final_topic=primary_draft.get("title"),
                # 그 편이 실제로 고른 방향. 편마다 검증을 다시 돌리면 글에는 마지막 편의
                # 후보만 남아, 자리번호로는 앞 편이 무엇을 골랐는지 되찾을 수 없다
                # (2026-08-12 사용자 신고 — validate_chosen_intent 주석).
                intent=primary_draft.get("intent"),
            )
        for draft in additional_drafts:
            clone = await self._blog_tasks.clone_for_direction(
                post_id,
                draft["intentId"],
                # 편마다 제목이 다르다(2026-08-12). 주지 않으면 원본 제목을 그대로 쓴다 —
                # 예약 경로의 "같은 제목을 여러 각도로"가 그대로 남는다.
                final_topic=draft.get("title"),
                intent=draft.get("intent"),
            )
            post_ids.append(clone.post_id)

        # 줄을 세운다. 첫 편만 시각을 보고, 뒤 편은 **앞 편이 끝나야** 시작한다
        # (worker._due_to_prepare). publish_at은 표시·정렬용으로만 남긴다 — 뒤 편이
        # 실제로 언제 돌지는 앞 편이 얼마나 걸리느냐에 달렸고, 미리 알 수 없다.
        # **이번 등록이 한 묶음이다**(2026-08-13). 이 경로는 돌고 있는 배치에 계속
        # 붙으므로, 배치나 소재로 세면 앞서 건 글까지 한 줄에 서서 '6편째'가 나온다.
        series_id = new_series_id()
        jobs: list[ScheduledJob] = []
        for index, each_post_id in enumerate(post_ids):
            jobs.append(
                ScheduledJob(
                    job_id=new_job_id(),
                    batch_id=batch_id,
                    user_id=user_id,
                    platform=SchedulePlatform.NAVER,
                    sequence=index,
                    topic=task.input.topic,
                    series_id=series_id,
                    post_id=each_post_id,
                    starts_from_prepared_post=True,
                    after_job_id=jobs[-1].job_id if jobs else None,
                    # 소재 단계의 '자동 발행'을 그대로 따른다(2026-08-12). 꺼 두면 원고까지만
                    # 만들고 작업 큐에 세워 둔다 — 사람이 보고 올린다.
                    publish_naver=task.input.auto_publish_naver,
                    publish_threads=task.input.auto_publish_threads,
                    publish_at=publish_at,
                    starts_immediately=starts_immediately,
                    timezone=task.input.scheduled_timezone,
                    created_at=now,
                    updated_at=now,
                )
            )
        job = jobs[0]

        if active is not None:
            existing_jobs = await self._repository.list_jobs(active.batch_id)
            base = len(existing_jobs)
            # **add_jobs로 만든다**(2026-08-13). 예전에는 save_job을 불렀는데 그쪽은
            # 일부러 upsert하지 않아서(삭제한 작업이 되살아나는 것을 막는 장치다),
            # 여기서 만든 작업이 하나도 저장되지 않았다 — 돌고 있는 배치가 있으면
            # 새 글 작성에서 건 예약이 조용히 사라졌다.
            await self._repository.add_jobs(
                [
                    each.model_copy(update={"sequence": base + offset})
                    for offset, each in enumerate(jobs)
                ]
            )
            total = base + len(jobs)
            await self._repository.save_batch(
                active.model_copy(
                    update={
                        "total_count": total,
                        "target_count": max(active.target_count, total),
                        "updated_at": now,
                    }
                )
            )
            await self._log(
                active.batch_id,
                _added_message(task.input.topic, len(jobs)),
                tone="info",
                job_id=jobs[0].job_id,
            )
            return await self._view(await self._require_batch(user_id, active.batch_id))

        batch = ScheduledBatch(
            batch_id=job.batch_id,
            user_id=user_id,
            platform=SchedulePlatform.NAVER,
            topic_mode=ScheduleTopicMode.MULTI,
            schedule_mode=ScheduleMode.ABSOLUTE,
            timezone=task.input.scheduled_timezone,
            publish_naver=True,
            publish_threads=False,
            status=ScheduledBatchStatus.READY,
            target_count=len(jobs),
            interval_seconds=DEFAULT_BATCH_INTERVAL_SECONDS,
            total_count=len(jobs),
            created_at=now,
            updated_at=now,
            logs=[
                ScheduledLogEntry(
                    at=now,
                    message=_scheduled_message(task.input.topic, len(jobs)),
                    tone="info",
                )
            ],
        )
        await self._repository.create_batch(batch, jobs)
        return await self._view(batch)

    # ------------------------------------------------------------ 일시정지·정지

    async def _is_executing(self, batch: ScheduledBatch) -> bool:
        """지금 이 배치가 글을 쓰거나 발행하고 있는가.

        배치가 RUNNING이어도 대개는 다음 발행까지 **기다리는 중**이다. 그 사이에는
        새 예약으로 갈아타도 잃을 것이 없다.
        """
        if batch.current_job_id is not None:
            return True
        jobs = await self._repository.list_jobs(batch.batch_id)
        return any(
            job.status in {ScheduledJobStatus.RUNNING, ScheduledJobStatus.PUBLISHING}
            for job in jobs
        )

    async def _supersede(self, batch: ScheduledBatch) -> None:
        """남은 배치를 닫고 비켜 준다. 이미 발행된 글은 그대로 둔다(되돌릴 수 없다)."""
        await self._cancel_waiting_jobs(batch.batch_id)
        await self._log(
            batch.batch_id, "새 예약을 시작해 이 배치는 여기서 마칩니다.", tone="muted"
        )
        await self._finish_batch(batch.batch_id, ScheduledBatchStatus.STOPPED)

    async def _require_batch(self, user_id: str, batch_id: str) -> ScheduledBatch:
        batch = await self._repository.find_user_batch(user_id, batch_id)
        if batch is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 배치 {batch_id}를 찾을 수 없습니다.")
        return batch

    async def request_pause(self, user_id: str, batch_id: str) -> ScheduledBatchView:
        batch = await self._require_batch(user_id, batch_id)
        if batch.status not in {ScheduledBatchStatus.READY, ScheduledBatchStatus.RUNNING}:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", "진행 중인 예약만 일시정지할 수 있습니다."
            )
        now = now_iso()
        # 실행 중인 LLM·Selenium 호출은 끊지 않는다. 지금 단계가 끝난 뒤 다음으로
        # 넘어가지 않게 표시만 남긴다.
        updated = batch.model_copy(
            update={
                "pause_requested": True,
                "status": ScheduledBatchStatus.PAUSE_REQUESTED,
                "updated_at": now,
            }
        )
        await self._repository.save_batch(updated)
        await self._log(batch_id, "사용자가 예약 작업을 일시정지했습니다.", tone="muted")
        return await self._view(await self._require_batch(user_id, batch_id))

    async def resume(self, user_id: str, batch_id: str) -> ScheduledBatchView:
        batch = await self._require_batch(user_id, batch_id)
        resumable = {
            ScheduledBatchStatus.PAUSED,
            ScheduledBatchStatus.PAUSE_REQUESTED,
            ScheduledBatchStatus.NEEDS_HUMAN,
        }
        if batch.status not in resumable:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", "일시정지되었거나 인증이 필요한 예약만 재개할 수 있습니다."
            )
        now = now_iso()
        # 새 배치를 만들지 않는다. 같은 batchId·jobId·postId를 그대로 쓴다.
        updated = batch.model_copy(
            update={
                "pause_requested": False,
                "status": ScheduledBatchStatus.RUNNING,
                "paused_at": None,
                # 재개는 곧바로 이어 간다 — 멈춰 있던 시간이 이미 간격을 채웠다.
                "next_run_at": None,
                "updated_at": now,
            }
        )
        await self._repository.save_batch(updated)
        # NEEDS_HUMAN으로 멈춰 있던 작업을 다시 대기로 되돌린다. 원고는 그대로 두므로
        # 발행 단계부터 이어진다.
        for job in await self._repository.list_jobs(batch_id):
            if job.status == ScheduledJobStatus.NEEDS_HUMAN:
                await self._repository.save_job(
                    job.model_copy(
                        update={
                            "status": ScheduledJobStatus.WAITING,
                            "error_code": None,
                            "error_message": None,
                            "updated_at": now,
                        }
                    )
                )
        await self._log(batch_id, "예약 작업을 재개합니다.", tone="info")
        return await self._view(await self._require_batch(user_id, batch_id))

    async def request_stop(self, user_id: str, batch_id: str) -> ScheduledBatchView:
        batch = await self._require_batch(user_id, batch_id)
        if batch.status not in ACTIVE_BATCH_STATUSES:
            raise ScheduledPostingError("VALIDATION_FAILED", "이미 끝난 예약입니다.")
        now = now_iso()
        await self._repository.save_batch(
            batch.model_copy(
                update={
                    "stop_requested": True,
                    "status": ScheduledBatchStatus.STOP_REQUESTED,
                    "updated_at": now,
                }
            )
        )
        # 대기 중인 작업은 여기서 바로 취소한다. 실행 중인 것은 안전한 지점에서 멈춘다.
        await self._cancel_waiting_jobs(batch_id)
        await self._log(batch_id, "사용자가 예약 작업을 정지했습니다.", tone="muted")

        # 지금 돌고 있는 작업이 없으면 즉시 닫는다. 있으면 그 작업이 끝나며 닫는다.
        refreshed = await self._require_batch(user_id, batch_id)
        if refreshed.current_job_id is None:
            await self._finish_batch(batch_id, ScheduledBatchStatus.STOPPED)
        return await self._view(await self._require_batch(user_id, batch_id))

    async def discard(self, user_id: str, batch_id: str) -> None:
        """배치를 버리고 처음 상태로 돌아간다 — '새 예약 시작' 버튼(2026-08-04 사용자 결정).

        정지와 다르다: 미완료 작업(대기·실패·취소·인증 대기)을 **DB에서 지운다**. 완료된
        작업은 남긴다 — 이미 네이버에 올라간 글의 기록이라, 지운다고 게시물이 사라지지
        않으므로 무엇이 발행됐는지는 남아야 한다. 완료가 하나도 없으면 배치째 지운다.

        **실행 중이어도 막지 않는다**(처음에는 거절했는데 사용자가 재확인했다 — "작업을
        하고 있더라도 중지가 되고 새 예약으로 되어야해"). 도는 단계를 끊지는 않는다:
        파이프라인은 단계 사이마다 작업 기록이 있는지 확인하므로(_check_control), 기록이
        사라진 것을 보고 다음 안전 지점에서 스스로 멈춘다. 즉시 끊지 않는 이유는
        발행(Selenium)을 중간에 죽이면 네이버에 올라갔는지 알 수 없는 글이 생기기
        때문이다 — 그래서 **발행이 이미 시작된 글 하나는 끝까지 가서 네이버에 남을 수
        있다**(그 기록은 '내 글 목록'의 발행 이력에 남는다).

        곧바로 '예약 시작'을 눌러도 안전하다: 워커는 작업을 한 번에 하나씩 돌리므로,
        새 배치의 첫 작업은 멈추는 중인 작업이 안전 지점에 닿은 뒤에 시작된다.
        """
        batch = await self._require_batch(user_id, batch_id)

        jobs = await self._repository.list_jobs(batch_id)
        removed = 0
        for job in jobs:
            if job.status == ScheduledJobStatus.COMPLETED:
                continue
            await self._repository.delete_job(job.job_id)
            # 이 작업이 만들다 만 글(blogTask)도 함께 지운다. 남겨 두면 '내 글 목록'에
            # '원고 준비 중' 카드가 쌓인다(2026-08-04 실사용 — "완료된 작업이 아닌
            # 것들은 DB에 남아있으면 안 되는데"). 실행 중이던 작업의 글은 파이프라인이
            # 멈추는 안전 지점에서 지운다(_check_control) — 여기서 지우면 도는 중인
            # 원고 생성이 지운 글을 계속 쓴다.
            if job.job_id != batch.current_job_id:
                await self._delete_backing_post(job.user_id, job.post_id)
            removed += 1

        completed = [job for job in jobs if job.status == ScheduledJobStatus.COMPLETED]
        if not completed:
            await self._repository.delete_batch(batch_id)
            logger.info("예약 배치 폐기 | %s - 작업 %d건 삭제", short(batch_id), removed)
            return

        now = now_iso()
        await self._repository.save_batch(
            batch.model_copy(
                update={
                    "status": ScheduledBatchStatus.STOPPED,
                    "current_job_id": None,
                    "next_run_at": None,
                    "stop_requested": True,
                    "pause_requested": False,
                    "total_count": len(completed),
                    "target_count": len(completed),
                    "completed_count": len(completed),
                    "failed_count": 0,
                    "canceled_count": 0,
                    "completed_at": batch.completed_at or now,
                    "updated_at": now,
                }
            )
        )
        await self._log(
            batch_id,
            f"새 예약을 위해 남은 작업 {removed}건을 지웠습니다. 발행된 글 기록만 남습니다.",
            tone="muted",
        )
        logger.info(
            "예약 배치 폐기 | %s - 작업 %d건 삭제, 완료 %d건 보존",
            short(batch_id),
            removed,
            len(completed),
        )

    async def _cancel_waiting_jobs(self, batch_id: str) -> int:
        now = now_iso()
        canceled = 0
        for job in await self._repository.list_jobs(batch_id):
            if job.status != ScheduledJobStatus.WAITING:
                continue
            await self._repository.save_job(
                job.model_copy(
                    update={"status": ScheduledJobStatus.CANCELED, "updated_at": now}
                )
            )
            canceled += 1
        if canceled:
            batch = await self._repository.find_batch(batch_id)
            if batch is not None:
                await self._repository.save_batch(
                    batch.model_copy(
                        update={
                            **await self._counts_of(batch_id),
                            "updated_at": now,
                        }
                    )
                )
        return canceled

    # ------------------------------------------------------------------ 재시도

    async def retry_job(self, user_id: str, job_id: str) -> ScheduledBatchView:
        job = await self._repository.find_user_job(user_id, job_id)
        if job is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 작업 {job_id}를 찾을 수 없습니다.")
        if job.status == ScheduledJobStatus.COMPLETED:
            # 성공한 작업은 다시 돌리지 않는다 — 같은 원고가 두 번 올라간다.
            raise ScheduledPostingError("VALIDATION_FAILED", "이미 완료된 작업입니다.")
        if job.status in {ScheduledJobStatus.RUNNING, ScheduledJobStatus.PUBLISHING}:
            raise ScheduledPostingError("VALIDATION_FAILED", "지금 실행 중인 작업입니다.")

        now = now_iso()
        await self._repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.WAITING,
                    "error_code": None,
                    "error_message": None,
                    "retry_count": job.retry_count + 1,
                    "scheduled_at": now,
                    "updated_at": now,
                }
            )
        )
        batch = await self._require_batch(user_id, job.batch_id)
        # 끝난 배치를 다시 열어 준다 — 실패 하나 때문에 배치가 닫혔을 수 있다.
        if batch.status not in ACTIVE_BATCH_STATUSES:
            batch = batch.model_copy(
                update={"status": ScheduledBatchStatus.RUNNING, "completed_at": None}
            )
        await self._repository.save_batch(
            batch.model_copy(
                update={
                    # 방금 이 작업이 실패(또는 취소)에서 대기로 돌아갔다. 개수를 다시
                    # 세지 않으면 그 실패가 배치에 남아, 재시도가 성공했을 때 같은
                    # 작업이 실패 1 · 완료 1로 두 번 세어진다(_counts_of 참고).
                    **await self._counts_of(job.batch_id),
                    "stop_requested": False,
                    "pause_requested": False,
                    "next_run_at": None,
                    "updated_at": now,
                }
            )
        )
        await self._log(job.batch_id, f"'{job.topic}' 작업을 다시 시도합니다.", tone="info")
        return await self._view(await self._require_batch(user_id, job.batch_id))

    # -------------------------------------------------- 예약 시각 변경·취소·목록

    async def reschedule_job(
        self, user_id: str, job_id: str, raw_body: Any
    ) -> ScheduledBatchView:
        """예약 하나의 발행 시각·시간대·플랫폼을 바꾼다.

        발행 중이거나 이미 발행된 글은 거부한다 — 올라가는 중인 글의 예약을 고치면
        화면과 실제 게시물이 다른 말을 하게 되고, 이미 올라간 글의 시각을 미래로 옮기면
        '아직 안 올라간 글'로 보이는데 네이버에는 이미 있다.

        실패했던 작업에 새 시각을 주면 다시 살아난다. 원고를 이미 만들어 뒀으면 발행만
        기다리는 자리(READY_TO_PUBLISH)로, 아직이면 대기로 되돌린다.
        """
        request = validate_reschedule_request(raw_body)
        job = await self._repository.find_user_job(user_id, job_id)
        if job is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 작업 {job_id}를 찾을 수 없습니다.")
        if job.status not in RESCHEDULABLE_JOB_STATUSES:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", _not_reschedulable_message(job.status)
            )
        # 아무 데도 안 올라가는 예약이 되지 않게, **바꾼 뒤의 상태**로 본다 — 한쪽만 보낸
        # 요청은 나머지가 지금 값 그대로다.
        naver = job.publish_naver if request.publish_naver is None else request.publish_naver
        threads = (
            job.publish_threads if request.publish_threads is None else request.publish_threads
        )
        if not naver and not threads:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", "발행할 플랫폼을 하나 이상 선택해 주세요."
            )
        # 연결 확인은 **이번에 켠 플랫폼만** 본다. 이미 켜져 있던 것까지 다시 보면,
        # 시각만 옮기려던 사용자가 예전에 고른 플랫폼 때문에 막힌다.
        if request.publish_naver and not _naver_saved(user_id):
            raise ScheduledPostingError(
                "NAVER_NOT_CONNECTED", "설정에서 Naver 계정을 먼저 저장해 주세요."
            )

        # 시각을 옮기면 **같은 배치의 다른 예약과 너무 붙을 수 있다.** 시작 화면에서만
        # 간격을 보면 여기로 우회해 1분 간격 예약을 만들 수 있다(2026-08-07). 아직
        # 올라가지 않은 것들만 본다 — 이미 발행됐거나 취소된 예약의 시각은 앞으로의
        # 발행 순서에 아무 영향이 없다.
        if request.publish_at is not None:
            siblings = [
                other.publish_at
                for other in await self._repository.list_jobs(job.batch_id)
                if other.job_id != job.job_id
                and other.publish_at
                and other.status
                not in {
                    ScheduledJobStatus.COMPLETED,
                    ScheduledJobStatus.CANCELED,
                }
            ]
            ensure_publish_gap([request.publish_at, *siblings])

        now = now_iso()
        updates: dict[str, Any] = {"updated_at": now}
        if request.publish_at is not None:
            updates["publish_at"] = request.publish_at
            # 시각을 고른 순간 '지금 바로'가 아니게 된다(2026-08-13). 표시를 지우지 않으면
            # 사용자가 시각을 정해 준 작업이 계속 '즉시 먼저' 줄에 남는다.
            updates["starts_immediately"] = False
        if request.timezone is not None:
            updates["timezone"] = request.timezone
        if request.publish_naver is not None:
            updates["publish_naver"] = request.publish_naver
        if request.publish_threads is not None:
            updates["publish_threads"] = request.publish_threads

        # 시각을 새로 준 작업은 다시 살아난다. 원고가 이미 있으면 발행만 기다리면 되고,
        # 없으면 처음부터다. 실패 사유와 자동 재시도 횟수는 여기서 지운다 — 새 예약이다.
        if request.publish_at is not None and job.status in {
            ScheduledJobStatus.FAILED,
            ScheduledJobStatus.NEEDS_HUMAN,
        }:
            updates["status"] = (
                ScheduledJobStatus.READY_TO_PUBLISH
                if job.generated_at
                else ScheduledJobStatus.WAITING
            )
            updates["error_code"] = None
            updates["error_message"] = None
            updates["publish_attempts"] = 0

        await self._repository.save_job(job.model_copy(update=updates))

        batch = await self._require_batch(user_id, job.batch_id)
        # 새 시각을 준 실패 작업은 방금 되살아났다 — 재시도와 같은 이유로 다시 센다.
        batch_updates: dict[str, Any] = {
            **await self._counts_of(job.batch_id),
            "updated_at": now,
        }
        if request.publish_at is not None:
            # 간격으로 돌던 배치에 절대 시각을 주면 그 배치는 절대 시각 방식이 된다.
            batch_updates["schedule_mode"] = ScheduleMode.ABSOLUTE
            if batch.status not in ACTIVE_BATCH_STATUSES:
                # 실패·정지로 닫힌 배치를 다시 연다. 예약을 고쳤다는 것은 다시 돌리겠다는 뜻이다.
                batch_updates["status"] = ScheduledBatchStatus.RUNNING
                batch_updates["completed_at"] = None
                batch_updates["stop_requested"] = False
                batch_updates["pause_requested"] = False
        if request.timezone is not None and batch.timezone is None:
            batch_updates["timezone"] = request.timezone
        await self._repository.save_batch(batch.model_copy(update=batch_updates))

        if request.publish_at is not None:
            await self._log(
                job.batch_id,
                f"'{job.topic}' 예약 시각을 바꿨습니다.",
                tone="info",
                job_id=job.job_id,
            )
        return await self._view(await self._require_batch(user_id, job.batch_id))

    async def cancel_job(self, user_id: str, job_id: str) -> ScheduledBatchView:
        """예약 하나를 취소한다. **문서는 지우지 않는다.**

        삭제(delete_job)와 다른 동작이다. 삭제는 큐에서 빼고 만들다 만 글까지 지우지만,
        취소는 "이 예약은 올리지 않는다"는 사실을 남긴다 — 목록에서 취소됨으로 계속
        보이고, 만들어 둔 원고도 그대로 있어 사용자가 나중에 손으로 발행할 수 있다.
        """
        job = await self._repository.find_user_job(user_id, job_id)
        if job is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 작업 {job_id}를 찾을 수 없습니다.")
        if job.status == ScheduledJobStatus.COMPLETED:
            raise ScheduledPostingError(
                "VALIDATION_FAILED",
                "이미 발행된 글입니다. 취소해도 게시물은 사라지지 않으니 네이버에서 직접 지워 주세요.",
            )
        if job.status == ScheduledJobStatus.PUBLISHING:
            raise ScheduledPostingError(
                "VALIDATION_FAILED", "지금 발행하는 중입니다. 끝난 뒤에 확인해 주세요."
            )
        if job.status == ScheduledJobStatus.CANCELED:
            # 두 번 눌렀다. 오류로 만들 이유가 없다 — 이미 원하는 상태다.
            return await self._view(await self._require_batch(user_id, job.batch_id))

        now = now_iso()
        await self._repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.CANCELED,
                    "error_code": None,
                    "error_message": None,
                    "updated_at": now,
                }
            )
        )
        await self._log(
            job.batch_id, f"'{job.topic}' 예약을 취소했습니다.", tone="muted", job_id=job.job_id
        )

        remaining = await self._repository.list_jobs(job.batch_id)
        batch = await self._repository.find_batch(job.batch_id)
        if batch is None:
            raise ScheduledPostingError("NOT_FOUND", "예약 배치를 찾을 수 없습니다.")
        batch = batch.model_copy(
            update={
                **await self._counts_of(job.batch_id),
                # 취소한 작업이 지금 돌던 작업이었을 리는 없지만(위에서 막았다), 배치가
                # 이 작업을 붙잡고 있으면 다음 작업이 시작되지 않는다.
                "current_job_id": (
                    None if batch.current_job_id == job.job_id else batch.current_job_id
                ),
                "updated_at": now,
            }
        )
        await self._repository.save_batch(batch)
        # 남은 것이 없으면 배치를 닫는다(삭제와 같은 규칙).
        await self._settle_after_delete(batch, remaining)

        refreshed = await self._repository.find_batch(job.batch_id)
        return await self._view(refreshed or batch)

    async def list_scheduled_jobs(
        self, user_id: str, limit: int = 50
    ) -> list[ScheduledJobListItem]:
        """이 사용자의 예약 목록 — 배치를 넘나들며 발행 시각 순으로.

        활성 배치 조회(get_active_batch)와 다른 화면을 위한 것이다. 그쪽은 '지금 돌고
        있는 예약 한 벌'이고, 이쪽은 '내가 걸어 둔 예약 전부'다. 배치가 끝나도 무엇이
        언제 올라갔는지 남아 있어야 한다.
        """
        jobs = await self._repository.list_user_jobs(user_id, limit=limit)

        # **왕복 두 번으로 끝낸다.** 예전에는 작업마다 글 한 편(제목을 얻으려고 원고
        # 전체)과 배치 하나를 따로 읽어, 작업 18건이면 조회가 30번 넘게 나갔다. 이 목록은
        # 화면이 2초마다 다시 부르는 것이라, 그 비용이 그대로 서버를 눌러 **작업 큐가
        # 갱신되지 않았다** — 글이 만들어지는 중인데도 화면은 '대기'였다(2026-08-06 신고).
        #
        # 조회 하나가 제목·글 상태·발행 주소·진행 칸을 한꺼번에 가져온다(PostSummary).
        # 예전에는 제목만 받았는데, 그러면 화면이 **작업의 상태밖에 몰라** 실패로 끝난
        # 작업의 글이 사실은 완성돼 있어도 그렇게 말할 수 없었다.
        summaries = await self._summaries_of({job.post_id for job in jobs if job.post_id})
        statuses = await self._batch_statuses({job.batch_id for job in jobs})

        items = []
        for job in jobs:
            summary = summaries.get(job.post_id or "")
            items.append(
                ScheduledJobListItem(
                    job=job,
                    title=summary.title if summary else None,
                    batch_status=statuses.get(job.batch_id),
                    post_status=summary.status if summary else None,
                    published_url=summary.published_url if summary else None,
                    progress=summary.progress if summary else None,
                    # 촘촘한 진행 줄. 이것을 옮겨 담지 않아 화면의 '작업 현황'이
                    # 단계 경계의 몇 줄만 보여 줬다(2026-08-12 사용자 신고).
                    activity_log=list(summary.activity_log) if summary else [],
                )
            )
        return items

    async def _summaries_of(self, post_ids: set[str]) -> dict[str, Any]:
        """글들의 요약(제목·상태·발행 주소·진행 칸).

        못 읽어도 목록은 보여 준다 — 한 줄의 곁들이 정보 때문에 예약 목록 전체를
        잃는 것이 더 나쁘다(제목만 읽던 시절부터의 정책 그대로다).
        """
        if not post_ids:
            return {}
        try:
            return await self._blog_tasks.get_post_summaries(sorted(post_ids))
        except Exception as error:  # noqa: BLE001 - 곁들이 정보 때문에 목록을 죽이지 않는다
            logger.warning("예약 목록의 글 상태 조회 실패 | %s", error)
            return {}

    async def _batch_statuses(self, batch_ids: set[str]) -> dict[str, ScheduledBatchStatus]:
        if not batch_ids:
            return {}
        try:
            return await self._repository.statuses_of_batches(sorted(batch_ids))
        except Exception as error:  # noqa: BLE001 - 상태를 못 읽어도 목록은 보여 준다
            logger.warning("예약 목록의 배치 상태 조회 실패 | %s", error)
            return {}

    # ------------------------------------------------------------------ 삭제

    async def forget_post(self, user_id: str, post_id: str) -> int:
        """지워진 글을 가리키던 예약 기록을 함께 없앤다. 손댄 배치의 수를 돌려준다.

        글을 지우는 쪽(``DELETE /posts/{id}``)이 부른다. **반대 방향은 이미 있었다** —
        예약 작업을 지우면 만들다 만 글이 함께 지워진다(``_delete_backing_post``). 그런데
        글을 지울 때는 예약 기록이 그대로 남아, 내 글 목록을 전부 비워도 발행 내역에는
        예전 줄이 그대로 있었다(2026-08-06 신고). 그 줄들이 화면에 적는 제목·상태·발행
        주소는 전부 그 글에서 읽어 오던 것이라, 글이 없으면 아무것도 설명하지 못한다.

        **게시물은 건드리지 않는다.** 네이버·스레드에 올라간 글은 그대로 있고, 여기서
        사라지는 것은 '언제 무엇을 예약했는가'라는 우리 쪽 기록 한 줄이다 — 글을
        지우겠다고 한 사람은 그 기록도 지우겠다는 뜻이다.

        도는 중인 작업도 지운다. 글이 이미 없어졌으므로 그 작업은 다음 단계에서 어차피
        실패한다 — 실패로 남겨 두는 것보다 함께 없애는 쪽이 사실에 가깝다.
        """
        batch_ids = await self._repository.delete_jobs_for_post(user_id, post_id)
        for batch_id in batch_ids:
            batch = await self._repository.find_batch(batch_id)
            if batch is None:
                continue
            remaining = await self._repository.list_jobs(batch_id)
            # 집계는 늘 남은 작업에서 다시 센다(하나씩 빼면 언젠가 어긋난다).
            # 화면의 '3건 중 n건'이 total_count를 읽으므로 그것도 함께 맞춘다 —
            # delete_job과 같은 규칙이다(0은 검증이 허용하지 않아 target은 그대로 둔다).
            await self._repository.save_batch(
                batch.model_copy(
                    update={
                        **await self._counts_of(batch_id),
                        "total_count": len(remaining),
                        "target_count": (
                            min(batch.target_count, len(remaining))
                            if remaining
                            else batch.target_count
                        ),
                        "updated_at": now_iso(),
                    }
                )
            )
        return len(batch_ids)

    async def delete_job(self, user_id: str, job_id: str) -> ScheduledBatchView:
        """작업 하나를 큐(또는 발행 내역)에서 뺀다.

        소재 1·2·3을 넣어 두고 2만 빼면 1·3만 이어서 쓰이게 하는 것이 목적이다.
        빼고 나면 순서는 남은 것들끼리 그대로다 — 워커는 sequence 순으로 다음 WAITING을
        집으므로 번호를 다시 매길 필요가 없다.

        **발행된 작업도 지울 수 있다**(2026-08-06 사용자 요청 — 발행 내역을 "직접
        깔끔하게 관리"). 지워지는 것은 **예약 기록 한 줄**뿐이다: 네이버·스레드의
        게시물은 그대로 있고, 원고도 '내 글 목록'에 남는다(``_delete_backing_post``가
        어딘가에 성공적으로 올라간 글은 지우지 않는다). 예전에는 이 자리를 막아 뒀는데,
        그러면 몇 주치 기록이 쌓인 뒤 화면을 정리할 방법이 아예 없었다.
        """
        job = await self._repository.find_user_job(user_id, job_id)
        if job is None:
            raise ScheduledPostingError("NOT_FOUND", f"예약 작업 {job_id}를 찾을 수 없습니다.")

        if job.status in {ScheduledJobStatus.RUNNING, ScheduledJobStatus.PUBLISHING}:
            # 도는 중인 LLM·Selenium을 버리면 네이버에 올라갔는지 알 수 없는 글이 남는다.
            raise ScheduledPostingError(
                "VALIDATION_FAILED",
                "지금 글을 쓰거나 발행하는 중인 작업입니다. 끝난 뒤에 삭제해 주세요.",
            )

        await self._repository.delete_job(job_id)
        # 만들다 만 글도 함께 지운다 — 큐에서 뺐는데 '내 글 목록'에 '원고 준비 중' 카드가
        # 남으면 결국 같은 잔재다(discard와 같은 정책, 발행된 글 기록은 지우지 않는다).
        await self._delete_backing_post(job.user_id, job.post_id)
        await self._log(job.batch_id, f"'{job.topic}' 작업을 목록에서 뺐습니다.", tone="muted")

        # 남은 작업을 **먼저** 읽고 배치는 그 뒤에 읽는다. 순서를 바꾸면 그 사이에 끝난
        # 작업이 배치에 쓴 값(next_run_at·current_job_id·완료 개수)을 낡은 사본으로
        # 통째로 덮어써, 간격을 무시하고 다음 글이 곧바로 올라간다.
        remaining = await self._repository.list_jobs(job.batch_id)
        batch = await self._repository.find_batch(job.batch_id)
        if batch is None:
            raise ScheduledPostingError("NOT_FOUND", "예약 배치를 찾을 수 없습니다.")

        now = now_iso()
        # 글의 개수는 남은 작업 수를 넘을 수 없다. 다 뺐으면(0) 마지막 값을 그대로 둔다 —
        # 0은 검증이 허용하지 않는 값이고, 빈 배치는 바로 아래에서 닫힌다.
        target_count = min(batch.target_count, len(remaining)) if remaining else batch.target_count
        # 개수는 남은 작업에서 다시 센다. 하나씩 빼고 더하면 언젠가 어긋난다.
        batch = batch.model_copy(
            update={
                "total_count": len(remaining),
                "target_count": target_count,
                "completed_count": sum(
                    1 for j in remaining if j.status == ScheduledJobStatus.COMPLETED
                ),
                "failed_count": sum(
                    1 for j in remaining if j.status == ScheduledJobStatus.FAILED
                ),
                "canceled_count": sum(
                    1 for j in remaining if j.status == ScheduledJobStatus.CANCELED
                ),
                "updated_at": now,
            }
        )
        await self._repository.save_batch(batch)

        await self._settle_after_delete(batch, remaining)

        refreshed = await self._repository.find_batch(job.batch_id)
        return await self._view(refreshed or batch)

    async def _settle_after_delete(
        self, batch: ScheduledBatch, remaining: list[ScheduledJob]
    ) -> None:
        """작업을 뺀 뒤 배치가 갈 곳을 정한다.

        '남은 작업이 0개'만 보면 안 된다. 완료·실패만 남고 **돌 것이 하나도 없는** 배치가
        열린 채로 남으면, 워커는 다음 실행 시각(최대 24시간 뒤)까지 잠들고 화면은 끝나지
        않은 예약을 계속 보여 준다.
        """
        if batch.status not in ACTIVE_BATCH_STATUSES:
            return

        runnable = any(job.status == ScheduledJobStatus.WAITING for job in remaining)
        busy = any(
            job.status
            in {
                ScheduledJobStatus.RUNNING,
                ScheduledJobStatus.PUBLISHING,
                ScheduledJobStatus.READY_TO_PUBLISH,
            }
            for job in remaining
        )
        if runnable or busy:
            # 인증을 기다리게 만든 작업을 지웠는데 돌 것이 남았다면 배치를 풀어 준다.
            # 안 그러면 워커가 NEEDS_HUMAN 배치를 통째로 건너뛰어 큐가 멈춘 채 있는다.
            blocked = any(
                job.status == ScheduledJobStatus.NEEDS_HUMAN for job in remaining
            )
            if batch.status == ScheduledBatchStatus.NEEDS_HUMAN and not blocked:
                await self._repository.save_batch(
                    batch.model_copy(
                        update={
                            "status": ScheduledBatchStatus.RUNNING,
                            "paused_at": None,
                            "updated_at": now_iso(),
                        }
                    )
                )
                await self._log(
                    batch.batch_id, "인증을 기다리던 작업을 빼 예약을 이어 갑니다.", tone="info"
                )
            return

        # 돌 것이 없다. 하나라도 발행했으면 완료, 아니면 멈춘 것으로 닫는다.
        done = any(job.status == ScheduledJobStatus.COMPLETED for job in remaining)
        await self._finish_batch(
            batch.batch_id,
            ScheduledBatchStatus.COMPLETED if done else ScheduledBatchStatus.STOPPED,
        )

    # ------------------------------------------------------------------ 로그

    async def _delete_backing_post(self, user_id: str, post_id: str | None) -> None:
        """예약 작업이 만들다 만 글(blogTask)을 지운다. best-effort.

        **어딘가에 성공적으로 올라간 글은 지우지 않는다** — 목록에서 지워도 게시물은
        남으므로, 무엇이 발행됐는지의 기록을 없애는 셈이 된다. 네이버든 쓰레드든 같다
        (쓰레드 단독 예약도 있다). 삭제 실패는 discard 자체를 막지 않는다(경고만 남긴다).
        """
        if not post_id:
            return
        try:
            task = await self._blog_tasks.get_blog_task(post_id)
            if task is not None and (
                _successful_naver_log(task) is not None
                or _successful_threads_log(task) is not None
            ):
                return
            await self._blog_tasks.delete_user_blog_task(user_id, post_id)
        except Exception as error:
            logger.warning(
                "예약 작업의 글 삭제 실패(글이 목록에 남습니다) | %s - %s", short(post_id), error
            )

    async def _log(self, batch_id: str, message: str, tone: str = "info", job_id=None) -> None:
        await self._repository.append_batch_log(
            batch_id,
            ScheduledLogEntry(at=now_iso(), message=message, tone=tone, job_id=job_id),
        )

    async def _job_log(self, job: ScheduledJob, message: str, tone: str = "info") -> None:
        """작업 한 건에 대한 로그. **어느 소재의 글인지 반드시 앞에 적는다.**

        한 배치의 글들이 이어서 돌기 때문에, 소재 없이 '제목을 자동 선택했습니다'만
        적으면 그것이 어느 글의 이야기인지 알 수 없다. 실제로 두 글의 단계가 섞여
        찍혀 있었다(2026-08-06 사용자 신고):

            19시 41분 8초   제목을 자동 선택했습니다.
            19시 40분 54초  소재 관련 키워드를 선택했습니다.
            19시 40분 33초  '무한리필'의 글 생성을 시작합니다.
            19시 40분 30초  '신라면'의 글 생성을 시작합니다.

        시작 줄에만 소재가 있어, 그 뒤 줄들이 신라면의 것인지 무한리필의 것인지 알 수
        없다. 이제 작업에 딸린 모든 줄이 이 메서드를 거친다 — 소재가 빠질 수 없다.

        메시지는 ``'{소재}'의 ...``에 이어 붙는 형태로 적는다(예: "제목을 자동
        선택했습니다" → "'신라면'의 제목을 자동 선택했습니다").
        """
        await self._log(
            job.batch_id, f"'{job.topic}'의 {message}", tone=tone, job_id=job.job_id
        )

    # ------------------------------------------------------------ 배치 마무리

    async def _finish_batch(self, batch_id: str, status: ScheduledBatchStatus) -> None:
        batch = await self._repository.find_batch(batch_id)
        if batch is None:
            return
        now = now_iso()
        await self._repository.save_batch(
            batch.model_copy(
                update={
                    "status": status,
                    "current_job_id": None,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
        )

    # ------------------------------------------------------------ 작업 실행

    async def _set_stage(self, job: ScheduledJob, stage: ScheduledJobStage) -> ScheduledJob:
        updated = job.model_copy(update={"stage": stage, "updated_at": now_iso()})
        await self._repository.save_job(updated)
        return updated

    async def _check_control(
        self,
        batch_id: str,
        job_id: str | None = None,
        *,
        cleanup: tuple[str, str | None] | None = None,
    ) -> None:
        """안전한 지점마다 정지·일시정지·삭제를 확인한다.

        실행 중인 외부 호출을 끊지 않는 대신, 단계 사이에서만 본다. 그래서 발행 버튼을
        누르는 도중에 정지를 눌러도 그 발행은 끝까지 간다.

        작업이 사라졌으면(사용자가 '새 예약 시작'·작업 삭제로 지웠으면) 거기서 멈춘다 —
        원고까지 만들었더라도 지운 소재를 네이버에 올리지는 않는다. ``cleanup``으로
        ``(user_id, post_id)``가 넘어왔으면 **만들다 만 글도 그때 함께 지운다**: 도는
        중에는 discard가 글을 지울 수 없으므로(원고 생성이 지운 글에 계속 쓴다), 멈추는
        바로 이 지점이 지울 수 있는 가장 이른 때다.

        취소된 예약도 같은 자리에서 멈춘다. 취소는 문서를 남기므로 '작업이 사라졌는가'
        만으로는 잡히지 않는데, 취소를 누른 뒤에도 글이 올라가면 취소 기능의 뜻이 없다.
        만들던 글은 지우지 않는다 — 취소는 기록을 남기는 동작이다(cancel_job 참고).
        """
        if job_id is not None:
            current = await self._repository.find_job(job_id)
            if current is None:
                if cleanup is not None:
                    await self._delete_backing_post(cleanup[0], cleanup[1])
                raise _Stopped()
            if current.status == ScheduledJobStatus.CANCELED:
                logger.info("취소된 예약이라 여기서 멈춥니다 | %s", short(job_id))
                raise _Canceled()
        batch = await self._repository.find_batch(batch_id)
        if batch is None:
            if cleanup is not None:
                await self._delete_backing_post(cleanup[0], cleanup[1])
            raise _Stopped()
        if batch.stop_requested:
            raise _Stopped()
        if batch.pause_requested:
            raise _Paused()

    async def execute_job(self, job_id: str, *, publish: bool = True) -> ScheduledJob | None:
        """작업 하나를 끌고 간다. 워커가 부른다.

        같은 작업을 두 번 발행하지 않도록, 각 구간은 **이미 끝나 있으면 건너뛴다**:
        postId가 있으면 글을 다시 만들지 않고, finalPost가 있으면 원고를 다시 쓰지 않고,
        네이버 성공 로그가 있으면 발행하지 않는다.

        ``publish=False``면 원고까지만 만들고 발행 직전에서 멈춘다(READY_TO_PUBLISH).
        절대 시각 예약이 이 모드로 미리 준비해 두었다가, 약속한 시각에 같은 메서드를
        ``publish=True``로 다시 불러 **발행만** 한다 — 그때는 원고가 이미 있으므로 위의
        '끝난 구간은 건너뛴다' 규칙에 따라 생성 단계가 통째로 빠진다.
        """
        job = await self._repository.find_job(job_id)
        if job is None:
            return None
        # 취소·완료된 예약은 무슨 이유로 여기까지 불려 왔든 실행하지 않는다. 워커도
        # 상태를 보고 고르지만, 고른 뒤 실제로 집어 드는 사이에 사용자가 취소했을 수 있다.
        if job.status in {ScheduledJobStatus.CANCELED, ScheduledJobStatus.COMPLETED}:
            logger.info(
                "예약 작업을 실행하지 않습니다(%s) | %s", job.status.value, short(job_id)
            )
            return None
        batch = await self._repository.find_batch(job.batch_id)
        if batch is None:
            return None

        now = now_iso()
        job = job.model_copy(
            update={
                "status": ScheduledJobStatus.RUNNING,
                "started_at": job.started_at or now,
                "updated_at": now,
            }
        )
        await self._repository.save_job(job)
        # 집어 드는 사이에 사용자가 이 작업을 지웠을 수 있다. 저장이 남지 않았다는 것이
        # 곧 그 신호다(save_job은 없는 작업을 만들지 않는다). 여기서 멈춰야 지운 소재로
        # 글이 만들어지거나 네이버에 올라가지 않는다.
        if await self._repository.find_job(job.job_id) is None:
            logger.info("예약 작업이 실행 직전에 삭제됨 | %s", short(job.job_id))
            return None

        # 배치를 **여기서 다시 읽는다.** save_batch는 문서를 통째로 바꾸므로, 위에서
        # 읽어 둔 사본으로 쓰면 그 사이에 다른 작업이 배치에 남긴 값(완료 개수·로그)을
        # 되돌린다. 절대 시각 예약은 원고를 여러 편 동시에 만들기 때문에(2026-08-07)
        # 그 '사이'가 실제로 자주 생긴다.
        fresh = await self._repository.find_batch(job.batch_id) or batch
        await self._repository.save_batch(
            fresh.model_copy(
                update={
                    "status": ScheduledBatchStatus.RUNNING,
                    "current_job_id": job.job_id,
                    "started_at": fresh.started_at or now,
                    "updated_at": now,
                }
            )
        )

        try:
            return await self._run_pipeline(job, publish=publish)
        except _Canceled:
            # 상태는 이미 CANCELED다. 그대로 두고 배치의 '지금 이 작업' 표시만 비운다.
            return await self._park(job, ScheduledJobStatus.CANCELED)
        except _Stopped:
            logger.info("예약 작업 정지 | %s", short(job.job_id))
            return await self._park(job, ScheduledJobStatus.WAITING)
        except _Paused:
            logger.info("예약 작업 일시정지 | %s", short(job.job_id))
            return await self._park(job, ScheduledJobStatus.WAITING)
        except BlogTaskError as error:
            await self._log(
                job.batch_id, f"'{job.topic}' 작업이 실패했습니다 — {error.message}", tone="muted"
            )
            return await self._fail(job, error.code, error.message)
        except Exception as error:  # noqa: BLE001 - 어떤 실패도 배치를 통째로 죽이지 않는다
            logger.warning(
                "예약 작업 실패 | %s - %s: %s",
                short(job.job_id),
                type(error).__name__,
                error,
                exc_info=True,
            )
            await self._log(job.batch_id, f"'{job.topic}' 작업이 실패했습니다.", tone="muted")
            return await self._fail(job, "SCHEDULED_JOB_FAILED", "예약 작업을 마치지 못했습니다.")

    async def _park(self, job: ScheduledJob, status: ScheduledJobStatus) -> ScheduledJob:
        """멈춤 — 실패가 아니다. 다음 재개에서 이 자리부터 이어 간다.

        배치의 '지금 이 작업이 돌고 있다' 표시도 반드시 함께 비운다. 워커의
        ``_reconcile_controls``는 current_job_id가 비어 있을 때만 정지·일시정지를
        최종 상태로 옮기므로, 여기서 안 비우면 배치가 STOP_REQUESTED에 영영 갇히고
        사용자는 서버를 다시 켤 때까지 새 예약을 시작하지 못한다.
        """
        current = await self._repository.find_job(job.job_id) or job
        now = now_iso()
        updated = current.model_copy(update={"status": status, "updated_at": now})
        await self._repository.save_job(updated)

        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None and batch.current_job_id == job.job_id:
            await self._repository.save_batch(
                batch.model_copy(update={"current_job_id": None, "updated_at": now})
            )
        return updated

    async def _fail(self, job: ScheduledJob, code: str, message: str) -> ScheduledJob:
        current = await self._repository.find_job(job.job_id) or job
        now = now_iso()
        updated = current.model_copy(
            update={
                "status": ScheduledJobStatus.FAILED,
                "error_code": code,
                "error_message": message,
                "updated_at": now,
            }
        )
        await self._repository.save_job(updated)
        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None:
            await self._repository.save_batch(
                batch.model_copy(
                    update={
                        **await self._counts_of(job.batch_id),
                        "current_job_id": None,
                        "updated_at": now,
                    }
                )
            )
        return updated

    async def _needs_human(self, job: ScheduledJob, message: str) -> ScheduledJob:
        """네이버가 사람을 부른다(캡차·2단계 인증). 실패로 뭉뚱그리지 않는다.

        기존 발행기가 열어 둔 Chrome은 그대로 있다 — 사용자가 그 창에서 인증을 마치고
        '재개'를 누르면 같은 원고로 발행만 다시 시도한다.

        **화면에는 발행기가 준 사유를 그대로 적는다.** 예전에는 무슨 일이 있었든
        "발행에 추가 인증이 필요합니다."로만 적어서, 이미 2단계 인증을 마친 사용자가
        왜 또 인증을 하라는지 알 수 없었다(2026-08-07 신고 — 실제 사유는 앞 발행이
        Chrome 프로필을 쓰고 있다는 것이었다).
        """
        current = await self._repository.find_job(job.job_id) or job
        now = now_iso()
        updated = current.model_copy(
            update={
                "status": ScheduledJobStatus.NEEDS_HUMAN,
                "error_code": "NAVER_NEEDS_HUMAN",
                "error_message": message,
                "updated_at": now,
            }
        )
        await self._repository.save_job(updated)
        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None:
            await self._repository.save_batch(
                batch.model_copy(
                    update={
                        "status": ScheduledBatchStatus.NEEDS_HUMAN,
                        "current_job_id": None,
                        "paused_at": now,
                        "updated_at": now,
                    }
                )
            )
        await self._job_log(
            job, (message or "").strip() or "발행에 추가 인증이 필요합니다.", tone="muted"
        )
        return updated

    # -------------------------------------------------------------- 파이프라인

    async def _run_pipeline(self, job: ScheduledJob, *, publish: bool = True) -> ScheduledJob:
        await self._check_control(job.batch_id, job.job_id)

        task = await self._ensure_post(job)
        job = await self._repository.find_job(job.job_id) or job
        # 이 지점부터는 글이 존재한다 — 멈출 때 만들다 만 글을 함께 지우기 위한 단서다.
        cleanup = (job.user_id, task.post_id)

        # 원고가 이미 있으면 M1~M4를 통째로 건너뛴다. 발행이 실패해 다시 들어온 경우가
        # 여기다 — 크롬이 죽은 것은 원고 품질 문제가 아니므로 다시 쓰지 않는다.
        if task.final_post is None:
            if job.starts_from_prepared_post:
                # 새 글 작성에서 넘어온 작업(2026-08-11). 소재·트렌드·제목은 사용자가 손으로
                # 정했으므로 건드리지 않는다 — 자동으로 다시 고르면 사용자가 고른 글이
                # 아니게 된다. 자료는 **지금** 모은다: 그것이 이 예약의 존재 이유다.
                #
                # 방향까지 골라 두고 넘어온 글(옛 흐름·사용자가 검증을 마친 경우)은 그
                # 방향을 유지하고 자료만 갈아끼운다.
                await self._check_control(job.batch_id, job.job_id, cleanup=cleanup)
                if task.selected_intent is None:
                    task = await self._ensure_intent(job, task)
                elif has_appointment(job):
                    task = await self._refresh_sources(job, task)
                # **시각을 정하지 않은 작업은 자료를 다시 모으지 않는다**(2026-08-13).
                #
                # 자료를 갈아끼우는 이유는 '며칠 뒤에 쓸 글의 자료가 낡는다'였다. 시각을
                # 정하지 않은 작업은 방금 검증 화면에서 모은 것이고, 사용자가 그 화면에서
                # 자료를 보고 **고르고 제외까지 했다**. 여기서 새로 모으면 그 선택이
                # 통째로 버려지고, 화면이 보여 준 자료와 원고가 쓴 자료가 달라진다.
            else:
                await self._check_control(job.batch_id, job.job_id, cleanup=cleanup)
                task = await self._ensure_trend_selection(job, task)

                await self._check_control(job.batch_id, job.job_id, cleanup=cleanup)
                task = await self._ensure_intent(job, task)

            await self._check_control(job.batch_id, job.job_id, cleanup=cleanup)
            task = await self._ensure_draft(job, task)

            await self._job_log(job, "원고와 이미지 생성이 완료되었습니다.")

        # 원고가 생긴 시각은 **원고가 있다는 사실**의 표시이기도 하다. 화면의 미리보기가
        # 이 값으로 열리므로, 이번 실행에서 만들었든 앞선 실행에서 만들었든(재개·복구)
        # 원고가 있으면 반드시 찍혀 있어야 한다.
        job = await self._repository.find_job(job.job_id) or job
        if task.final_post is not None and job.generated_at is None:
            job = job.model_copy(update={"generated_at": now_iso(), "updated_at": now_iso()})
            await self._repository.save_job(job)

        await self._check_control(job.batch_id, job.job_id, cleanup=cleanup)
        if not publishes_anywhere(job):
            # **올릴 곳을 하나도 고르지 않았다 — 원고를 만들었으면 이 작업은 끝이다**
            # (2026-08-13 사용자 지시: "플랫폼 선택안했는데 왜 발행을 하려고 그래.
            # 원고생성 완료하면 작업 끝이지").
            #
            # 예전에는 이런 작업도 READY_TO_PUBLISH('발행 대기')에 세웠다. 워커는 올릴
            # 곳이 없는 작업을 발행 후보에서 빼므로(_due_to_publish) 그 자리에서 영영
            # 움직이지 않고, 배치도 닫히지 않는다(_close_if_done은 READY_TO_PUBLISH를
            # '아직 진행 중'으로 본다). 화면에는 하지도 않을 발행을 기다리는 줄이 남았다.
            return await self._finish_without_publishing(job)
        if not publish:
            # 절대 시각 예약의 준비 단계. 원고는 다 만들었고, 약속한 시각이 될 때까지
            # 발행하지 않는다. 워커가 그 시각에 같은 작업을 publish=True로 다시 부른다.
            return await self._ready_to_publish(job)
        return await self._publish(job, task)

    async def _finish_without_publishing(self, job: ScheduledJob) -> ScheduledJob:
        """올릴 곳이 없는 작업을 원고까지로 마친다(2026-08-13).

        발행 시각(``published_at``)은 **찍지 않는다** — 올라간 적이 없다. 그 값이 있으면
        목록·발행 내역이 발행된 글로 읽는다.
        """
        return await self._complete(
            job,
            None,
            message="원고를 만들었습니다. 발행할 곳을 고르지 않아 여기서 마칩니다.",
            published=False,
        )

    async def _ready_to_publish(self, job: ScheduledJob) -> ScheduledJob:
        """원고까지 끝났다 — 발행 시각을 기다리는 자리에 세운다.

        배치의 '지금 이 작업이 돌고 있다' 표시를 반드시 비운다(_park과 같은 이유다).
        여기서 안 비우면 배치가 이 작업을 붙잡은 채로 남아 다음 준비가 시작되지 않는다.
        """
        current = await self._repository.find_job(job.job_id) or job
        now = now_iso()
        updated = current.model_copy(
            update={"status": ScheduledJobStatus.READY_TO_PUBLISH, "updated_at": now}
        )
        await self._repository.save_job(updated)

        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None:
            batch_updates: dict[str, Any] = {
                "current_job_id": (
                    None if batch.current_job_id == job.job_id else batch.current_job_id
                ),
                "updated_at": now,
            }
            if batch.schedule_mode is ScheduleMode.ABSOLUTE:
                # 다음 **원고 생성**은 지금부터 간격만큼 뒤에 시작한다. 발행 시각과는
                # 별개의 설정이다 — LLM·크롬을 한꺼번에 여러 개 돌리지 않기 위한 것이다.
                batch_updates["next_run_at"] = _after_seconds(batch.interval_seconds)
            # 간격 방식에서는 건드리지 않는다(2026-08-10). 그 값은 '직전 발행 성공 +
            # 간격'이라는 **발행 게이트**인데, 준비 완료가 이 값을 밀면 병렬로 준비될
            # 때마다 첫 발행이 그만큼 늦어진다.
            await self._repository.save_batch(batch.model_copy(update=batch_updates))
        when = (
            # 새 글 작성에서 넘어온 작업은 원고가 끝나는 대로 올린다(2026-08-11) —
            # 시각을 적으면 기다린다는 뜻이 되는데 실제로는 기다리지 않는다.
            "곧 발행합니다."
            if job.starts_from_prepared_post
            else f"{_when_label(updated.publish_at)} 발행합니다."
            if batch is None or batch.schedule_mode is ScheduleMode.ABSOLUTE
            # 간격 방식은 시각이 아니라 순서가 발행을 정한다 — 거짓 시각을 적지 않는다.
            else "앞 글이 발행되면 순서대로 발행합니다."
        )
        await self._log(
            job.batch_id,
            f"'{job.topic}' 원고를 준비했습니다. {when}",
            tone="info",
            job_id=job.job_id,
        )
        return updated

    async def _ensure_post(self, job: ScheduledJob):
        """BlogTask를 만든다. 이미 만들었으면 그것을 다시 쓴다."""
        if job.post_id:
            task = await self._blog_tasks.get_blog_task(job.post_id)
            if task is not None:
                return task
            # postId는 있는데 글이 없다(사용자가 지웠다). 새로 만드는 것이 맞다.
            logger.info("예약 작업의 글이 사라졌습니다 — 다시 만듭니다 | %s", short(job.job_id))

        await self._set_stage(job, ScheduledJobStage.CREATE_POST)
        # 소재 분야는 **고른 작업에만** 싣는다. 빈 값을 보내면 create_blog_task가
        # 목록 밖의 값이라고 거절하고, 그러면 옛 작업이 통째로 실패한다.
        creation: dict[str, Any] = {
            "userId": job.user_id,
            "topic": job.topic,
            "purpose": list(SCHEDULED_DEFAULT_PURPOSE),
        }
        if job.subject_category:
            creation["subjectCategory"] = job.subject_category

        # 브랜드를 건 배치의 작업은 그 자료를 얹는다(2026-08-19). 화면에서 만드는 글과
        # **같은 함수**를 쓴다 — 여기서 따로 구현하면 자동 포스팅으로 나간 글만 브랜드
        # 처리가 다르게 되고, 그 차이는 원고가 나오고 나서야 보인다.
        #
        # 소재가 반드시 있는 흐름이라 역할은 언제나 활용(UTILITY)이다. 결합 가능성도
        # 그 함수가 재어 글에 남기므로, 억지 조합이면 원고가 브랜드를 마지막에 한 번만
        # 언급하게 된다(brand_utility_rules).
        #
        # 상한을 **브랜드가 있을 때만** 넘긴다. 브랜드 자료는 서버가 펼쳐 넣는 것이라
        # 기본 상한으로는 자료를 다 채운 브랜드가 글을 만들지 못하는데, 브랜드가 없는
        # 배치까지 상한을 얹으면 이 호출의 모양이 예전과 달라진다 — 브랜드를 쓰지 않는
        # 예약은 여기서 한 글자도 달라지지 않아야 한다.
        if job.brand_id and self._brands is not None:
            creation["brandId"] = job.brand_id
            creation, limit = await with_brand_materials(
                self._brands, job.user_id, creation
            )
            task = await self._call_with_retry(
                lambda: self._blog_tasks.create_blog_task(creation, limit)
            )
        else:
            task = await self._call_with_retry(
                lambda: self._blog_tasks.create_blog_task(creation)
            )
        await self._repository.save_job(
            job.model_copy(update={"post_id": task.post_id, "updated_at": now_iso()})
        )
        # '글 생성을 시작합니다'는 여기서 남기지 않는다 — note_generation_start 참고.
        return task

    async def note_generation_start(self, job: ScheduledJob) -> None:
        """'글 생성을 시작합니다' 한 줄. **워커가 입력 순서대로 미리** 부른다.

        예전에는 준비 태스크 안(_ensure_post)에서 각자 남겼다. 준비가 병렬이 되면서
        두 태스크가 글 생성·작업 저장·로그를 각자 DB로 오가고, 먼저 끝난 쪽이 로그를
        먼저 썼다 — 그래서 **둘째 소재가 먼저 시작한 것처럼 보였다**(2026-08-10 신고:
        청년주택·애슐리 퀸즈 순으로 넣었는데 애슐리가 위에 찍혔다).

        띄우는 순서(sequence)는 처음부터 맞았고 발행 순서도 게이트가 지킨다 —
        어긋난 것은 **보이는 순서 하나**였다. 그래서 태스크를 띄우기 전에 워커가
        차례로 한 줄씩 남기고, 그 뒤 준비는 예전처럼 병렬로 돈다.

        이미 글이 있는 작업(재시도)은 남기지 않는다 — 예전 _ensure_post가 그 경우
        일찍 돌아가 로그를 남기지 않던 것과 같다.
        """
        if job.post_id:
            return
        await self._job_log(job, "글 생성을 시작합니다.")

    async def _sibling_titles(self, job: ScheduledJob) -> list[str]:
        """같은 배치의 앞선 작업들이 이미 고른 제목.

        소재 하나로 여러 편을 쓸 때 제목이 겹치지 않게 하려고 기존 ``excludeTitles``에
        그대로 넘긴다 — 새 프롬프트를 만들지 않고 제목 생성기가 이미 받는 인자를 쓴다.
        같은 소재를 쓰는 형제만 본다(소재가 다르면 제목이 겹칠 일이 없다).
        """
        titles: list[str] = []
        for sibling in await self._repository.list_jobs(job.batch_id):
            if sibling.job_id == job.job_id or sibling.topic != job.topic:
                continue
            if not sibling.post_id:
                continue
            task = await self._blog_tasks.get_blog_task(sibling.post_id)
            chosen = getattr(getattr(task, "trend_selection", None), "final_topic", None)
            if chosen:
                titles.append(chosen)
        # 제목 생성기의 배제 목록에는 상한이 있다(MAX_EXCLUDE_TITLES). 최근 것부터 넣는다.
        return titles[-10:]

    async def _ensure_trend_selection(self, job: ScheduledJob, task):
        """M2. 소재 관련 키워드를 고르고 제목을 자동 선택한다.

        키워드가 하나도 없으면 임의로 지어내지 않고 기존 지원 방식으로 건너뛴다
        (``select_topic({"skipped": true})``) — 그러면 사용자가 입력한 소재가 그대로 제목이다.
        """
        if task.trend_selection is not None:
            return task
        if task.status != BlogTaskStatus.REFERENCE_PROCESSING:
            # 이미 M2를 지난 글이다(복구된 작업). 그대로 다음 단계로 간다.
            return task

        post_id = task.post_id
        await self._set_stage(job, ScheduledJobStage.TREND_RECOMMENDATION)
        keyword = None
        try:
            recommendation = await self._trends.recommend_topics(
                post_id,
                {
                    "mode": "MATERIAL_RELATED",
                    "maxKeywords": TREND_MAX_KEYWORDS,
                    "forceCollect": False,
                    "shuffle": False,
                },
            )
            keyword = pick_trend_keyword(
                list(recommendation.trend_keywords or []), job.variant_index
            )
        except BlogTaskError as error:
            if _is_concurrent_update(error):
                raise
            # 트렌드는 글을 쓰는 데 필수가 아니다. 못 모았으면 소재 그대로 간다.
            logger.info("예약: 트렌드 수집 실패 — 소재 그대로 진행 | %s", short(post_id))

        if keyword is None:
            await self._trends.select_topic(
                post_id, {"skipped": True, "selectedTrendKeywordIds": []}
            )
            await self._job_log(
                job, "관련 키워드가 없어 입력한 소재를 그대로 씁니다.", tone="muted"
            )
            return await self._require_task(post_id)

        # **고른 키워드를 그대로 적는다**(2026-08-06 사용자 요청). '키워드를 선택했습니다'
        # 만으로는 무엇을 골랐는지 알 수 없고, 그 선택이 제목·자료·원고의 방향을 정한다 —
        # 결과가 마음에 들지 않을 때 어디서 갈렸는지 볼 수 있는 유일한 자리다.
        await self._job_log(
            job, f"소재 관련 키워드를 선택했습니다: '{keyword.keyword}'"
        )
        await self._set_stage(job, ScheduledJobStage.TITLE_GENERATION)
        # 같은 배치의 앞선 글들이 이미 쓴 제목. 키워드가 변종 수보다 적어 돌려 쓰게 될 때
        # 이것이 차이를 만든다. 재시도 람다 밖에서 미리 구한다(람다 안에서는 await할 수 없다).
        exclude_titles = await self._sibling_titles(job)
        titles = await self._call_with_retry(
            lambda: self._trends.generate_topics(
                post_id,
                {
                    "trendKeywordId": keyword.trend_keyword_id,
                    "keyword": keyword.keyword,
                    "source": keyword.source,
                    "excludeTitles": exclude_titles,
                    "excludeAngles": [],
                    # 제목 생성기는 이 값으로 이번 회차의 방향을 고른다(title_variation).
                    # 변종 번호를 그대로 넘기면 같은 소재의 N편이 서로 다른 각도가 된다.
                    "regenerationCount": job.variant_index,
                },
            )
        )
        chosen = pick_title(list(titles.topic_candidates or []))
        if chosen is None:
            # 제목을 못 만들었으면 트렌드를 건너뛴다 — 지어낸 제목을 쓰지 않는다.
            await self._trends.select_topic(
                post_id, {"skipped": True, "selectedTrendKeywordIds": []}
            )
            return await self._require_task(post_id)

        await self._trends.select_topic(
            post_id,
            {
                "topicCandidateId": chosen.topic_candidate_id,
                "finalTopic": chosen.title,
                "selectedTrendKeywordIds": [keyword.trend_keyword_id],
                "selectedKeywords": [keyword.keyword],
                "hookType": getattr(chosen, "hook_type", None),
                "skipped": False,
            },
        )
        await self._job_log(job, f"제목을 자동 선택했습니다: '{chosen.title}'")
        return await self._require_task(post_id)

    async def _ensure_intent(self, job: ScheduledJob, task):
        """M3. 검색 의도를 검증하고 자동 선택한다."""
        if task.selected_intent is not None:
            return task

        post_id = task.post_id
        await self._set_stage(job, ScheduledJobStage.SEARCH_ANALYSIS)

        result = task.intent_validation_result
        if result is None or result.provider == INTENT_PROVIDER_FAILED:
            analyzed = await self._call_with_retry(
                lambda: self._blog_tasks.analyze_intent_candidates(post_id)
            )
            # 선언은 BlogTask지만 실제로는 None이 올 수 있다.
            if analyzed is None:
                raise ScheduledPostingError("NOT_FOUND", "글이 사라져 검증을 마치지 못했습니다.")
            task = analyzed
            result = task.intent_validation_result

        # 검증 실패는 status를 바꾸지 않는다 — provider가 유일한 신호다.
        if result is None or not result.intent_candidates:
            raise ScheduledPostingError(
                "INTENT_VALIDATION_FAILED", "검색 의도 검증에서 후보를 얻지 못했습니다."
            )
        if result.provider == INTENT_PROVIDER_FAILED:
            raise ScheduledPostingError(
                "INTENT_VALIDATION_FAILED",
                "검색 의도 검증에 실패해 원고를 만들지 않았습니다.",
            )
        if task.status == BlogTaskStatus.FAILED:
            raise ScheduledPostingError(
                "INTENT_VALIDATION_FAILED", "검색 분석 중 글이 실패 상태가 되었습니다."
            )

        chosen = pick_intent(list(result.intent_candidates))
        await self._set_stage(job, ScheduledJobStage.INTENT_SELECTION)
        await self._blog_tasks.select_intent(
            post_id, {"intentId": chosen.intent_id, "excludedSourceUrls": []}
        )
        await self._job_log(job, "검색 의도 검증이 완료되었습니다.")
        return await self._require_task(post_id)

    async def _refresh_sources(self, job: ScheduledJob, task):
        """예약 시각에 **자료만** 새로 모은다(2026-08-11). 방향은 사용자가 고른 그대로다.

        실패해도 진행한다 — 자료를 새로 못 모았다고 약속한 시각을 놓치는 것보다, 옛
        자료로 쓰고 그 사실을 남기는 쪽이 낫다(blog_task 서비스가 같은 판단을 한다).
        """
        await self._set_stage(job, ScheduledJobStage.SEARCH_ANALYSIS)
        # **'다시'라고 하지 않는다**(2026-08-12 사용자 지적: "애초에 안모으고 지정해둔
        # 원고생성 시간이 되면 수집하는거 아니야?"). 검증 단계에서 미리 모으던 것을
        # 없앴으므로(_collects_sources_now) 여기가 **처음** 모으는 자리다.
        await self._job_log(job, "원고를 만들 차례가 되어 최신 자료를 모으고 있습니다.")
        refreshed = await self._blog_tasks.refresh_selected_intent_sources(task.post_id)
        count = len(refreshed.selected_intent.sources or []) if refreshed.selected_intent else 0
        await self._job_log(job, f"최신 자료 {count}건으로 원고를 만듭니다.")
        return refreshed

    async def _ensure_draft(self, job: ScheduledJob, task):
        """M4. 기존 원고·이미지 생성기를 그대로 통과시킨다.

        ``generate_draft``는 실패해도 예외를 던지지 않으므로, 성공은 **상태로만** 본다.
        """
        if task.final_post is not None:
            return task

        post_id = task.post_id
        await self._set_stage(job, ScheduledJobStage.DRAFT_GENERATION)
        # 이 한 줄 뒤로 5~8분이 흐른다. 그 안의 네 칸(구조 설계 → 본문 → 카드 이미지 →
        # 사실 검수·문장 다듬기)은 작업 큐 줄이 실시간으로 보여 준다(jobProgressNote).
        await self._job_log(
            job, "원고와 이미지를 생성하고 있습니다(구조 설계 → 본문 → 이미지 → 다듬기)."
        )

        generated = await self._drafts.generate_draft(post_id, {"format": "html"})
        if generated is None:
            generated = await self._blog_tasks.get_blog_task(post_id)
        if generated is None:
            raise ScheduledPostingError("NOT_FOUND", "글이 사라져 원고를 마치지 못했습니다.")

        if generated.status == BlogTaskStatus.CONTENT_POLICY_VIOLATION:
            raise ScheduledPostingError(
                "CONTENT_POLICY_VIOLATION",
                "콘텐츠 정책에 걸려 발행하지 않았습니다.",
            )
        if generated.status == BlogTaskStatus.GENERATING:
            # 다른 프로세스가 임차를 쥐고 있다. 우리 것이 아니므로 기다리지 않는다.
            raise ScheduledPostingError(
                "DRAFT_IN_PROGRESS", "다른 작업이 이 글의 원고를 만들고 있습니다."
            )
        if generated.status != BlogTaskStatus.READY_TO_PUBLISH or generated.final_post is None:
            raise ScheduledPostingError("DRAFT_FAILED", "원고 생성에 실패했습니다.")
        return generated

    async def _begin_publish(self, job: ScheduledJob) -> ScheduledJob:
        """발행을 시작한다고 적는다 — 상태·마지막 시도 시각·시도 횟수.

        ``last_attempt_at``은 성공 시각(``published_at``)과 다르다 — 실패한 시도도 남아야
        "몇 시에 시도해서 안 됐는가"를 목록에서 볼 수 있다. 자동 재시도 한도를 세는
        ``publish_attempts``도 여기서 오른다.
        """
        now = now_iso()
        current = await self._repository.find_job(job.job_id) or job
        updated = current.model_copy(
            update={
                "status": ScheduledJobStatus.PUBLISHING,
                "last_attempt_at": now,
                "publish_attempts": current.publish_attempts + 1,
                "updated_at": now,
            }
        )
        await self._repository.save_job(updated)
        return updated

    async def _publish(self, job: ScheduledJob, task) -> ScheduledJob:
        """M5. 사용자가 고른 곳에 올린다 — 네이버만·쓰레드만·둘 다.

        둘 다면 **네이버가 먼저**다(스레드 실패로 네이버 발행을 잃지 않게).
        ``publish_naver``가 꺼진 작업은 네이버를 아예 건너뛴다(2026-08-06).

        중복 방지가 여기에 있다: 이 글에 네이버 자동 발행 **성공 기록**이 이미 있으면
        다시 올리지 않고 그 결과로 작업을 맞춘다(스레드도 같은 방식으로 따로 확인한다).
        """

        post_id = task.post_id
        if not job.publish_naver:
            # 쓰레드 단독 예약. 중복 확인은 _publish_threads가 스레드 기록으로 따로 한다.
            # 단계를 먼저 적는다 — 상태만 PUBLISHING으로 바꾸면 그 사이에 화면이
            # 'Naver 발행 중'이라고 말한다(단계가 아직 발행 전 칸이다).
            await self._set_stage(job, ScheduledJobStage.THREADS_PUBLISH)
            job = await self._begin_publish(job)
            return await self._publish_threads(job, None)

        existing = _successful_naver_log(task)
        if existing is not None:
            if job.publish_threads:
                return await self._publish_threads(job, existing.post_url)
            return await self._complete(job, existing.post_url, already=True)

        await self._set_stage(job, ScheduledJobStage.NAVER_PUBLISH)
        job = await self._begin_publish(job)
        await self._job_log(job, "네이버 발행을 시작합니다.")

        published = await self._blog_tasks.publish_blog_task(
            post_id,
            {
                "method": "auto",
                "channel": "naver",
            },
        )
        log = published.posting_logs[-1] if published.posting_logs else None
        if log is None:
            raise ScheduledPostingError("PUBLISH_FAILED", "발행 결과를 확인하지 못했습니다.")

        if log.result == PostingResultStatus.SUCCESS:
            if job.publish_threads:
                await self._job_log(job, "네이버 발행이 완료되었습니다.", tone="success")
                return await self._publish_threads(job, log.post_url)
            return await self._complete(job, log.post_url)
        if log.result == PostingResultStatus.NEEDS_HUMAN:
            return await self._needs_human(
                job, log.error_message or "네이버에서 추가 인증이 필요합니다."
            )
        # 발행에 실패했다. 원고는 그대로 남으므로 재시도는 발행만 다시 한다.
        await self._job_log(job, "네이버 발행에 실패했습니다.", tone="muted")
        return await self._fail_publish(
            job, "PUBLISH_FAILED", log.error_message or "네이버 발행에 실패했습니다."
        )

    async def _fail_publish(self, job: ScheduledJob, code: str, message: str) -> ScheduledJob:
        """발행 실패. 절대 시각 예약이고 시도 횟수가 남았으면 자동으로 다시 잡는다.

        여기 오는 것은 **발행기가 '실패'라고 분명히 말한 경우뿐이다.** 결과를 알 수 없는
        실패(발행 도중 서버가 죽는 것)는 이 자리에 오지 못하고 recovery.py가 사람 확인으로
        돌린다 — 그것까지 자동으로 다시 올리면 같은 글이 두 번 게시된다.
        """
        current = await self._repository.find_job(job.job_id) or job
        if current.publish_at is None or current.publish_attempts >= MAX_PUBLISH_ATTEMPTS:
            return await self._fail(current, code, message)

        now = now_iso()
        retry_at = _after_seconds(PUBLISH_RETRY_BACKOFF_SECONDS * current.publish_attempts)
        updated = current.model_copy(
            update={
                # 원고는 그대로 있으므로 발행만 기다리는 자리로 되돌린다.
                "status": ScheduledJobStatus.READY_TO_PUBLISH,
                "publish_at": retry_at,
                "error_code": code,
                "error_message": message,
                "updated_at": now,
            }
        )
        await self._repository.save_job(updated)
        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None and batch.current_job_id == job.job_id:
            await self._repository.save_batch(
                batch.model_copy(update={"current_job_id": None, "updated_at": now})
            )
        await self._log(
            job.batch_id,
            f"'{job.topic}' 발행을 {_when_label(retry_at)} 다시 시도합니다"
            f"({current.publish_attempts}/{MAX_PUBLISH_ATTEMPTS}회).",
            tone="muted",
            job_id=job.job_id,
        )
        return updated

    async def _publish_threads(self, job: ScheduledJob, naver_url: str | None) -> ScheduledJob:
        """원고를 스레드에 올린다. 네이버도 함께 고른 작업이면 네이버가 먼저다.

        작업의 post_url은 **네이버를 함께 올렸을 때만** 네이버 주소를 유지한다 —
        그때는 예약의 대표 주소가 그쪽이기 때문이다. 쓰레드 단독 예약이면 올릴 네이버
        주소가 없으므로 스레드 주소를 대표로 쓴다. 어느 쪽이든 채널별 주소는 blogTask의
        posting_logs에 그대로 남는다.

        네이버를 함께 올린 작업은 실패해도 네이버에는 이미 올라가 있다. 그래서 실패
        문구에 그 사실을 함께 적고, 재시도는 네이버 성공 기록 덕에 발행 단계로 와서
        스레드만 다시 올린다. 쓰레드 단독 예약은 올라간 곳이 없으므로 다른 발행 실패와
        똑같이 자동 재시도(_fail_publish)를 받는다.
        """
        task = await self._require_task(job.post_id)
        threads_done = _successful_threads_log(task)
        if threads_done is not None:
            # 재시도에서 스레드까지 이미 올라간 경우 — 다시 올리면 같은 글이 두 벌 생긴다.
            return await self._complete(
                job, naver_url or threads_done.post_url, already=True
            )

        await self._set_stage(job, ScheduledJobStage.THREADS_PUBLISH)
        await self._job_log(job, "스레드 발행을 시작합니다.")

        published = await self._blog_tasks.publish_blog_task(
            job.post_id,
            {
                "method": "auto",
                "channel": "threads",
            },
        )
        log = published.posting_logs[-1] if published.posting_logs else None
        if log is None:
            raise ScheduledPostingError("PUBLISH_FAILED", "스레드 발행 결과를 확인하지 못했습니다.")

        if log.result == PostingResultStatus.SUCCESS:
            return await self._complete(
                job, naver_url or log.post_url, message="스레드 발행이 완료되었습니다."
            )
        if log.result == PostingResultStatus.NEEDS_HUMAN:
            return await self._needs_human(
                job, log.error_message or "스레드에서 추가 인증이 필요합니다."
            )
        await self._job_log(job, "스레드 발행에 실패했습니다.", tone="muted")
        reason = log.error_message or "원인을 확인하지 못했습니다."
        if not job.publish_naver:
            # 쓰레드 단독 예약 — 아무 데도 올라가지 않았다. 다른 발행 실패와 같이
            # 자동 재시도를 받는다(절대 시각 예약이고 횟수가 남았을 때).
            return await self._fail_publish(
                job, "THREADS_PUBLISH_FAILED", f"스레드 발행에 실패했습니다: {reason}"
            )
        return await self._fail(
            job,
            "THREADS_PUBLISH_FAILED",
            f"스레드 발행에 실패했습니다(네이버에는 발행됨): {reason}",
        )

    async def _complete(
        self,
        job: ScheduledJob,
        post_url: str | None,
        already: bool = False,
        message: str | None = None,
        published: bool = True,
    ) -> ScheduledJob:
        """작업 하나를 끝낸다.

        ``published``가 False면 **발행 시각을 찍지 않는다** — 올릴 곳이 없어 원고까지만
        만들고 끝낸 작업이다(2026-08-13). 그 값이 있으면 목록·발행 내역이 올라간 글로
        읽는다.
        """
        current = await self._repository.find_job(job.job_id) or job
        now = now_iso()
        updated = current.model_copy(
            update={
                "status": ScheduledJobStatus.COMPLETED,
                "stage": ScheduledJobStage.DONE,
                "published_at": (current.published_at or now) if published else current.published_at,
                "post_url": post_url or current.post_url,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
        )
        await self._repository.save_job(updated)

        batch = await self._repository.find_batch(job.batch_id)
        if batch is not None:
            batch_updates: dict[str, Any] = {
                **await self._counts_of(job.batch_id),
                "current_job_id": None,
                "updated_at": now,
            }
            if batch.schedule_mode is not ScheduleMode.ABSOLUTE:
                # 간격 방식: 다음 작업은 **이 발행 성공 시각**을 기준으로 간격을 잰다.
                # 원고 생성이 오래 걸려도 두 발행 사이는 최소 간격만큼 벌어진다.
                batch_updates["next_run_at"] = _after_seconds(batch.interval_seconds)
            # 절대 시각 방식에서는 next_run_at을 건드리지 않는다. 그 값은 '다음 원고 생성을
            # 시작해도 되는 시각'이고 준비 단계가 이미 잡아 뒀다 — 여기서 다시 미루면
            # 발행이 끝날 때마다 생성이 한 번씩 더 밀린다.
            await self._repository.save_batch(batch.model_copy(update=batch_updates))
        if already:
            await self._job_log(
                job, "글이 이미 올라가 있어 발행을 건너뛰었습니다.", tone="muted"
            )
        else:
            await self._job_log(
                job, message or "네이버 발행이 완료되었습니다.", tone="success"
            )
        await self._announce_next_job(job.batch_id)
        return updated

    async def _announce_next_job(self, batch_id: str) -> None:
        """다음 작업이 언제인지 '발행 완료' 바로 뒤에 예고한다.

        예고가 없으면 '발행 완료' 뒤의 침묵이 배치가 끝난 것인지 기다리는 것인지
        구분되지 않는다(2026-08-04 사용자 요청). 시각은 이 PC의 지역 시간으로 적는다 —
        로그를 읽는 사람이 보는 시계가 그것이다.

        절대 시각 예약에서는 '다음 원고가 언제 시작되는가'가 아니라 **다음 글이 언제
        올라가는가**를 말한다. 사용자가 정한 것이 그쪽이기 때문이다.
        """
        batch = await self._repository.find_batch(batch_id)
        if batch is None or batch.stop_requested or batch.pause_requested:
            # 멈추라고 한 배치의 다음 작업은 시작되지 않는다 — 거짓 예고를 남기지 않는다.
            return
        pending = [
            job
            for job in await self._repository.list_jobs(batch_id)
            if job.status
            in {ScheduledJobStatus.WAITING, ScheduledJobStatus.READY_TO_PUBLISH}
        ]
        if not pending:
            return

        if batch.schedule_mode is ScheduleMode.ABSOLUTE:
            # 발행 시각이 가장 이른 것이 다음이다(sequence는 등록 시점의 순서라, 예약을
            # 고친 뒤에는 시각 순서와 어긋날 수 있다).
            next_job = min(pending, key=lambda job: job.publish_at or "")
            await self._log(
                batch_id,
                f"다음은 {_when_label(next_job.publish_at)} '{next_job.topic}' 발행입니다.",
                tone="info",
                job_id=next_job.job_id,
            )
            return

        next_job = next(
            (job for job in pending if job.status == ScheduledJobStatus.WAITING), None
        )
        if next_job is None:
            return
        due = _parse_iso(batch.next_run_at)
        when = f"{due.astimezone():%H시 %M분 %S초}에" if due else "곧"
        await self._log(
            batch_id,
            f"{when} '{next_job.topic}' 소재에 대한 원고 작업이 시작됩니다.",
            tone="info",
            job_id=next_job.job_id,
        )

    # ------------------------------------------------------------------ 보조

    async def _require_task(self, post_id: str):
        task = await self._blog_tasks.get_blog_task(post_id)
        if task is None:
            raise ScheduledPostingError("NOT_FOUND", f"blogTask {post_id} not found")
        return task

    async def _call_with_retry(self, call):
        """원고 생성 **전** 단계의 일시적 오류를 한 번만 다시 시도한다.

        발행에는 쓰지 않는다 — 결과가 불확실한 발행을 자동으로 다시 하면 중복 글이 생긴다.
        """
        attempt = 0
        while True:
            try:
                return await call()
            except BlogTaskError as error:
                if _is_concurrent_update(error) and attempt < PRE_DRAFT_RETRY_LIMIT:
                    # 낙관적 잠금 충돌은 상태 위반이 아니다. 잠깐 뒤 다시 읽으면 풀린다.
                    attempt += 1
                    await asyncio.sleep(PRE_DRAFT_RETRY_DELAY_SECONDS)
                    continue
                raise
            except Exception:
                if attempt >= PRE_DRAFT_RETRY_LIMIT:
                    raise
                attempt += 1
                await asyncio.sleep(PRE_DRAFT_RETRY_DELAY_SECONDS)


def _not_reschedulable_message(status: ScheduledJobStatus) -> str:
    if status == ScheduledJobStatus.PUBLISHING:
        return "지금 발행하는 중인 예약은 바꿀 수 없습니다. 끝난 뒤에 확인해 주세요."
    if status == ScheduledJobStatus.COMPLETED:
        return "이미 발행된 글의 예약은 바꿀 수 없습니다."
    if status == ScheduledJobStatus.RUNNING:
        return "지금 원고를 만드는 중입니다. 잠시 뒤에 다시 시도해 주세요."
    if status == ScheduledJobStatus.CANCELED:
        return "취소된 예약입니다. 다시 예약하려면 새로 등록해 주세요."
    return "이 상태의 예약은 바꿀 수 없습니다."


def _successful_naver_log(task) -> Any | None:
    """이 글에 네이버 자동 발행 성공 기록이 있으면 그 로그."""
    from app.shared import PostingChannel

    return _successful_channel_log(task, PostingChannel.NAVER)


def _successful_threads_log(task) -> Any | None:
    """이 글에 스레드 자동 발행 성공 기록이 있으면 그 로그. 재시도의 중복 방지 근거다."""
    from app.shared import PostingChannel

    return _successful_channel_log(task, PostingChannel.THREADS)


def _successful_channel_log(task, channel) -> Any | None:
    for log in reversed(list(getattr(task, "posting_logs", []) or [])):
        if (
            log.method == PostingMethod.AUTO
            and log.channel == channel
            and log.result == PostingResultStatus.SUCCESS
        ):
            return log
    return None
