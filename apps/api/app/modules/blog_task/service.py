"""블로그 글 작성 태스크: 생성, 조회, 상태 전이."""

import asyncio
import inspect
import logging
from app.shared.format import now_iso as _now
from typing import Any

from app.errors import BlogTaskError
from app.shared.ids import new_log_id, new_post_id, short
from app.llm import (
    ProviderContextExceededError,
    ProviderEmptyResponseError,
    ProviderOverloadedError,
    ProviderRefusedError,
    ProviderTruncatedError,
    WebSearchAnalyzer,
)
from app.posting import PublishJob, PublishResult
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskListItem,
    BlogTaskStatus,
    IntentCandidate,
    IntentValidationResult,
    PostingChannel,
    PostingLog,
    PostingMethod,
    PostingResultStatus,
    PostSummary,
    TaskPhase,
    WebSearchAnalysisInput,
)

from app.shared import perf

from .jobs import BackgroundJobs, ProgressReporter
from .locks import JobLease, NoOpJobLease, hold, lease_key
from .repository import BlogTaskRepository, BlogTaskStatusSnapshot

logger = logging.getLogger(__name__)
from .validation import (
    MAX_REFERENCE_MATERIALS,
    validate_blog_task_input,
    validate_create_blog_task_request,
    validate_select_intent_request,
)

REFERENCE_INTAKE_ACTOR = "system:m1-reference-intake"
INPUT_EDIT_ACTOR = "user:m1-input-edit"
POSTING_ACTOR = "system:m5-posting"
WEB_SEARCH_ACTOR = "system:m3-web-search"
INTENT_SELECTION_ACTOR = "user:m3-intent-selection"
# v1.2: 사용자가 준 URL을 문자열로만 검색하지 않고 Gemini URL Context로 직접 조회한다.
M3_PROMPT_VERSION = "m3-intent@v1.2"


def _validate_publish_request(body: Any) -> tuple[PostingMethod, PostingChannel]:
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")
    method = body.get("method")
    if method not in (
        PostingMethod.COPY.value,
        PostingMethod.DRAFT.value,
        PostingMethod.AUTO.value,
    ):
        raise BlogTaskError("VALIDATION_FAILED", "method must be copy, draft, or auto")
    # 채널이 없던 클라이언트의 요청은 전부 네이버였다 — 기본값이 과거와 같아야 한다.
    channel = body.get("channel", PostingChannel.NAVER.value)
    if channel not in (PostingChannel.NAVER.value, PostingChannel.THREADS.value):
        raise BlogTaskError("VALIDATION_FAILED", "channel must be naver or threads")
    if channel == PostingChannel.THREADS.value and method != PostingMethod.AUTO.value:
        # 스레드에는 임시저장이 없고, 복사는 채널 개념이 없다(클립보드가 목적지).
        raise BlogTaskError("VALIDATION_FAILED", "threads channel supports only auto publishing")
    return PostingMethod(method), PostingChannel(channel)


def _failure_reason(error: Exception) -> str:
    """사용자에게 보여 줄 실패 사유 한 줄.

    예전에는 `str(error)`를 그대로 실어, 카드에 제공자 원문 JSON이 그대로 떴다:

        provider request failed with 500: {"message": "gemini-3.5-flash is currently
        experiencing high demand, ...", "code": "api_error"}

    사용자가 할 수 있는 일을 판단하는 데 아무 도움이 안 되는 문장이다. 혼잡은 기다리면
    풀리고, 설정 오류는 눌러도 같은 화면이라는 것만 구분해 주면 된다. 기술적 원문은
    로그에 이미 남는다(검증 실패 warning).
    """
    if isinstance(error, ProviderOverloadedError):
        return (
            f"AI 제공자({error.provider})가 지금 혼잡해 자료를 모으지 못했습니다"
            " — 여러 번 다시 시도했지만 모두 같은 응답이었습니다. 잠시 뒤 다시 시도해 주세요"
        )
    # HTTP 200으로 왔지만 쓸 수 없는 응답(stop_reason). 사용자가 할 일이 셋 다 다르다.
    if isinstance(error, ProviderRefusedError):
        return (
            "AI 모델의 안전 정책 때문에 요청이 거절됐습니다"
            " — 소재나 참고자료를 바꿔 다시 시도해 주세요"
        )
    if isinstance(error, ProviderContextExceededError):
        return (
            "입력이 모델이 한 번에 읽을 수 있는 한도를 넘었습니다"
            " — 참고자료를 줄여 다시 시도해 주세요"
        )
    if isinstance(error, ProviderTruncatedError):
        return (
            "결과가 최대 출력 길이에 도달해 잘렸습니다"
            " — 잠시 뒤 다시 시도해 주세요. 계속 반복되면 설정을 확인해야 합니다"
        )
    if isinstance(error, ProviderEmptyResponseError):
        # 검색은 다 돌았는데 모델이 마지막 답을 쓰지 않았다(2026-08-12 사용자 신고).
        # 폴백 모델까지 전부 그랬을 때만 여기 온다 — 사용자가 할 일은 다시 누르는 것뿐이다.
        return (
            "AI가 자료는 찾았지만 정리한 내용을 돌려주지 않았습니다"
            " — 다른 모델로도 다시 시도했지만 같았습니다. '다시 검증'을 눌러 주세요"
        )
    reason = str(error).strip() or error.__class__.__name__
    return f"자료를 모으는 중 오류가 났습니다 — {reason[:200]}"


def _failed_validation_result(
    post_id: str, analysis_input: BlogTaskInput, error: Exception
) -> IntentValidationResult:
    """검증이 실패했다는 사실을 그대로 담은 결과.

    자료는 지어내지 않는다(빈 목록). 실패 사유를 rationale에 담아 검증 팝업이 '참고 자료'
    자리에 그대로 보여주게 한다 — 사용자가 왜 막혔는지 알고 다시 시도할지, 자료 없이
    진행할지 고를 수 있어야 한다.
    """
    reason = _failure_reason(error)
    return IntentValidationResult(
        prompt_version=M3_PROMPT_VERSION,
        provider="none",
        model="",
        analyzed_at=_now(),
        intent_candidates=[
            IntentCandidate(
                intent_id=f"{post_id}_intent_failed",
                title=analysis_input.topic,
                target_reader="블로그 독자",
                rationale=(
                    f"{reason}. "
                    "'다시 검증'을 눌러 다시 시도하거나, 참고 자료 없이 계속 진행할 수 있습니다."
                ),
                keywords=analysis_input.keywords,
                sources=[],
            )
        ],
    )


def _accepts_note(analyzer: Any) -> bool:
    """이 수집기가 진행 보고 콜백(on_note)을 받는가."""
    try:
        return "on_note" in inspect.signature(analyzer.search_and_analyze).parameters
    except (TypeError, ValueError):  # 시그니처를 읽을 수 없는 호출가능 객체
        return False


def _analysis_key(task: BlogTask) -> str:
    """검증(M3)이 무엇을 근거로 도는지 요약한 값.

    제목을 다시 고르면 이 값이 달라진다. 두 곳이 본다:
    - start_intent_analysis: 같은 근거의 잡이 돌고 있을 때만 중복으로 취급한다.
    - _run_intent_analysis_locked: 결과를 저장하기 직전, 글의 근거가 그대로인지 확인한다 —
      1분짜리 검색이 도는 동안 사용자가 제목을 바꿨다면 옛 제목의 결과를 버린다.
    """
    selection = task.trend_selection
    if selection is None:
        return f"input:{task.input.topic}"
    return "|".join(
        [
            "skipped" if selection.skipped else "picked",
            selection.final_topic or "",
            ",".join(selection.selected_keywords or []),
        ]
    )


#: 자료를 모으지 않는 실행의 칸 이름(2026-08-12). 그 실행이 하는 일은 방향을 나누는
#: 하나뿐이라, '자료 검색 → 검증 후보 정리' 두 칸을 그리면 거짓이 된다.
DIRECTIONS_ONLY_STEPS = ["글의 방향 나누기"]
DIRECTIONS_ONLY_PHASE_NAME = "검증(글의 방향)"


def _collects_sources_now(task_input: BlogTaskInput) -> bool:
    """검증(M3) 단계에서 **지금 자료를 모을 것인가**.

    가르는 것은 **작업 시각을 정했는가** 하나다(2026-08-13 사용자 지시).

    - **시각을 정한 글**: 모으지 않는다. 며칠 뒤에 쓸 글의 자료를 오늘 모으면 그 사이에
      나온 이슈가 빠진다 — 이 예약의 존재 이유가 바로 그 차이다(2026-08-11 결정).
      원고를 만들 때 새로 모은다(``refresh_selected_intent_sources``).
    - **시각을 정하지 않은 글**: 편수와 무관하게 **지금 모아 보여 준다.** 원고는 곧바로
      만들어지고, 사용자는 이 화면에서 자료를 보고 고른다.

    편수 조건은 걷어냈다(2026-08-13). 2026-08-12에는 "설정한 편수가 한편일때만 검증단계에서
    자료수집해서 사용자에게 보여주고 2편 이상으로 설정한 경우에는 자료수집은 원고생성 단계
    진입했을때"였는데, 그때는 여러 편이 **작업 큐에서 순서대로** 돌아 원고 시점이 한참
    뒤였다. 지금은 시각을 정하지 않은 여러 편이 곧바로 함께 돌아(worker._due_to_prepare)
    검증에서 모은 자료가 그대로 쓰인다 — 사용자가 화면에서 본 것이 실제로 쓰이는 자료다
    (사용자: "수집한 자료가 보여져야지").

    **방향 후보는 어느 쪽이든 그대로 만든다.** 사용자가 이 화면에서 고르는 것은 방향이고,
    그것은 자료 없이도 소재·목적·독자·제목으로 나눌 수 있다.
    """
    return not (task_input.scheduled_run_at or "").strip()


def _threads_topic_for(task: BlogTask) -> str:
    """스레드 작성창의 '커뮤니티 또는 주제'에 넣을 값.

    사용자가 트렌드에서 고른 검색어가 가장 구체적이라 그것을 먼저 쓰고, 없으면(트렌드를
    건너뛴 글·옛 문서) 입력한 소재를 쓴다. 블로그 카테고리를 쓰지 않는 이유는 저장된
    문서에 남아 있지 않고, 스레드의 그 칸이 고정 분류가 아니라 주제 태그이기 때문이다.
    """
    selection = task.trend_selection
    keywords = list(getattr(selection, "selected_keywords", None) or []) if selection else []
    for keyword in keywords:
        if keyword and keyword.strip():
            return keyword.strip()
    return (task.input.topic or "").strip()


def _has_successful_auto_publish(task: BlogTask, channel: PostingChannel) -> bool:
    """그 채널에 자동 발행 성공 기록이 있으면 같은 원고의 중복 게시를 막는다.

    가드는 채널별이다 — 스레드에 올린 글을 네이버에 올리는 것은 중복이 아니라 두 번째
    채널 발행이다. 채널 필드가 없던 옛 로그는 기본값(naver)으로 읽힌다.
    """
    return any(
        log.method == PostingMethod.AUTO
        and log.result == PostingResultStatus.SUCCESS
        and log.channel == channel
        for log in task.posting_logs
    )


def _retitled(selection, final_topic: str | None, now: str):
    """복제본이 쓸 제목 확정(2026-08-12).

    ``final_topic``이 없으면 원본의 것을 그대로 쓴다 — 예약 경로("같은 제목을 여러
    각도로")가 예전과 똑같이 동작한다. 있으면 제목만 갈아 끼우고 **나머지는 원본 그대로**
    둔다: 어떤 트렌드 키워드에서 나왔는지·건너뛴 것인지는 이 글에서도 같은 사실이다.
    """
    if selection is None or not final_topic or not final_topic.strip():
        return selection
    return selection.model_copy(
        update={"final_topic": final_topic.strip(), "selected_at": now}
    )


def _with_chosen_candidate(
    result: IntentValidationResult | None, chosen: IntentCandidate
) -> IntentValidationResult:
    """그 편이 **실제로 고른 방향**을 검증 결과 안에 되돌려 놓는다(2026-08-12).

    왜 필요한가: 한 소재로 여러 편을 만들면 편마다 제목을 다시 골라 검증(M3)이 다시 돈다.
    새 제목을 저장하는 ``save_trend_selection``이 옛 검증 결과를 지우므로, 마지막에 예약을
    걸 시점의 글에는 **마지막 편의 후보만** 남는다. 앞 편의 자리번호(``{postId}_intent_{n}``)는
    거기서 다른 방향을 가리키거나 아예 없다 — 그대로 두면 고르지도 않은 방향으로 원고가
    만들어진다(2026-08-12 사용자 신고).

    **덮어쓰지 않고 앞에 놓는다.** 같은 자리번호의 것만 밀어내고 나머지 후보는 남긴다 —
    화면이 그 글의 검증 결과를 열어 볼 때 정보가 줄어들 이유가 없다.
    """
    others = [
        candidate
        for candidate in (result.intent_candidates if result else [])
        if candidate.intent_id != chosen.intent_id
    ]
    if result is None:
        # 검증 결과가 아예 없는 글(제목을 다시 골라 지워진 직후)도 있다. 고른 방향 하나로
        # 결과를 세운다 — 출처는 화면이 보고 고른 그 방향이다.
        return IntentValidationResult(
            prompt_version="",
            provider="",
            model="",
            analyzed_at=_now(),
            intent_candidates=[chosen],
        )
    return result.model_copy(update={"intent_candidates": [chosen, *others]})


class BlogTaskService:
    # 옛 제목의 검증이 임차를 쥔 채 도는 동안 새 제목의 검증이 기다리는 상한(초).
    # M3 한 벌이 1~2분이므로 그보다 넉넉하게 잡는다.
    _M3_LEASE_WAIT_SECONDS = 300.0

    def __init__(
        self,
        repository: BlogTaskRepository,
        posting_worker: Any,
        web_search_analyzer: WebSearchAnalyzer,
        job_lease: JobLease | None = None,
        threads_writer: Any | None = None,
        user_settings_service: Any | None = None,
    ):
        self._repository = repository
        self._posting_worker = posting_worker
        self._web_search_analyzer = web_search_analyzer
        # 스레드 전용 게시물을 쓰는 생성기(generate_threads_post). 없으면 발행기가
        # 블로그 원고를 잘라 담는 예전 방식으로 폴백한다 — 테스트·구형 구성에서도
        # 발행은 돈다.
        self._threads_writer = threads_writer
        # 스레드를 몇 개로 나눌지는 글 길이 설정이 정한다(짧게 2~3 / 중간 3~5).
        # 없으면 생성기가 중간 규격으로 폴백한다 — 설정을 못 읽었다고 발행을 막지 않는다.
        self._user_settings = user_settings_service
        self._jobs = BackgroundJobs()
        # 같은 글의 검증을 두 프로세스가 동시에 돌리지 않게 하는 임차. Redis가 없으면
        # 항상 잡히는 no-op이다(프로세스가 하나뿐이면 중복이 생기지 않는다).
        self._lease = job_lease or NoOpJobLease()
        # 같은 글의 M3 잡을 이 프로세스 안에서 두 번 띄우지 않는다. 상태 전이가 잠금
        # 역할을 하는 M4·M5와 달리, M3은 이미 SEARCH_ANALYZING인 글에 다시 들어올 수
        # 있어(M2가 먼저 옮겨 놓기 때문) 상태만으로는 중복을 막지 못한다. 프로세스가
        # 여럿일 때는 위의 임차(JobLease)가 같은 일을 한다.
        #
        # 글 하나가 아니라 "무엇을 근거로 도는 검증인지"(_analysis_key)를 담는다. 제목을
        # 다시 고르면 근거가 달라지므로, 돌고 있던 검증은 더 이상 이 글의 것이 아니다 —
        # 그때는 중복이 아니라 새 검증이어야 한다.
        self._m3_inflight: dict[str, str] = {}
        # 글별 검증 잡 핸들. 기다리는 쪽이 **자기 글만** 기다리게 하려는 것이다.
        self._m3_jobs: dict[str, Any] = {}

    async def shutdown(self) -> None:
        await self._jobs.cancel()

    async def _require_task(self, post_id: str) -> BlogTask:
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        return task

    async def create_blog_task(
        self, raw_body: Any, max_reference_materials: int = MAX_REFERENCE_MATERIALS
    ) -> BlogTask:
        """글을 만든다.

        ``max_reference_materials``는 **서버가 자료를 조립해 넣는 경로**만 올려 잡는다
        (브랜드 글이 그렇다 — 저장해 둔 브랜드 자료가 참고자료로 펼쳐져 들어간다).
        화면이 직접 보내는 입력은 기본값 그대로 10개다.
        """
        request = validate_create_blog_task_request(raw_body, max_reference_materials)
        now = _now()
        post_id = new_post_id()

        task = BlogTask(
            post_id=post_id,
            user_id=request.user_id,
            status=BlogTaskStatus.INPUT,
            version=1,
            created_at=now,
            updated_at=now,
            status_history=[],
            input=request.input,
            posting_logs=[],
        )

        await self._repository.create(task)
        return await self._begin_reference_processing(task)

    async def update_blog_task_input(
        self,
        post_id: str,
        raw_body: Any,
        max_reference_materials: int = MAX_REFERENCE_MATERIALS,
    ) -> BlogTask:
        """진행 중인 글의 입력을 다시 쓴다 — 수정하기가 하는 일.

        옛 입력에서 파생된 모든 것을 버리고 글은 흐름의 처음으로 돌아간다. 한 주제로
        추천된 트렌드는 다른 주제에 대해 아무것도 말해 주지 않기 때문이다.

        ``max_reference_materials``는 생성 경로와 같은 이유로 열어 둔다 — 브랜드를 고른
        글은 서버가 브랜드 자료를 펼쳐 넣어서 화면 상한(10개)을 넘는다.
        """
        task = await self._require_task(post_id)

        # 원고는 옛 입력으로 쓰였다. 그 아래에서 입력만 다시 쓰면 본문이 더 이상 하지 않는
        # 질문에 답하는 글이 남는다 — 그리고 API는 한 번만 원고를 쓰므로 여기서 재생성은
        # 제공하지 않는다.
        if task.final_post is not None:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"blogTask {post_id} already has a draft; start a new post to change the input",
            )

        blog_input = validate_blog_task_input(raw_body, max_reference_materials)
        updated = await self._repository.replace_input(post_id, blog_input, INPUT_EDIT_ACTOR)

        return await self._begin_reference_processing(updated)

    async def get_blog_task(self, post_id: str) -> BlogTask | None:
        return await self._repository.find_by_post_id(post_id)

    async def get_post_summaries(self, post_ids: list[str]) -> dict[str, PostSummary]:
        """여러 글의 상태·제목·발행 주소·진행 칸·작업 현황 줄을 한 번에.

        예약 목록이 쓴다. 그 화면은 작업의 상태만 알고 있었는데, 그것은 작업이 끝났을
        때의 마지막 기억일 뿐 **글이 지금 어떤 상태인지**가 아니다(PostSummary 참고).

        '작업 현황' 줄은 DB가 아니라 프로세스 메모리에서 붙인다 — 폴링 화면이 늘 최신
        줄을 받게 하려는 것이고, 자리는 get_user_blog_task_status와 같다. 예약 화면이
        원고를 만드는 5~8분 동안 멈춘 것처럼 보이던 것을 푼다(2026-08-10 사용자 요청).
        """
        summaries = await self._repository.summaries_by_post_ids(post_ids)
        from .jobs import activity_log_for

        return {
            post_id: summary.model_copy(
                update={"activity_log": activity_log_for(post_id)}
            )
            for post_id, summary in summaries.items()
        }

    async def get_user_blog_task(self, user_id: str, post_id: str) -> BlogTask | None:
        """소유자의 DB 범위 안에서만 글을 반환한다.

        HTTP 호출자는 이 메서드를 쓴다. 그래서 소유권은 범위 없는 문서를 이미 읽은 뒤에
        하는 검사가 아니라 Mongo 쿼리의 일부가 된다.
        """
        return await self._repository.find_by_user_and_post_id(user_id, post_id)

    async def get_user_blog_task_status(
        self, user_id: str, post_id: str
    ) -> BlogTaskStatusSnapshot | None:
        """Return only the fields needed by the polling loop."""
        snapshot = await self._repository.find_status_by_user_and_post_id(user_id, post_id)
        if snapshot is None:
            return None
        # '작업 현황' 로그(2026-08-10)는 DB가 아니라 프로세스 메모리에서 온다 — 생성이
        # 도는 그 프로세스가 응답도 만들므로 폴링 화면에는 항상 최신 줄이 실린다.
        from .jobs import activity_log_for

        return snapshot.model_copy(update={"activity_log": activity_log_for(post_id)})

    async def user_owns_blog_task(self, user_id: str, post_id: str) -> bool:
        """소유권만 묻는다. 글 내용이 필요 없는 라우트(삭제·발행·생성 시작)가 쓴다.

        `get_user_blog_task`와 답은 같지만 **문서를 끌어오지 않는다**(repository.owns_post
        참고). 글 한 편이 몇 MB라 그 차이가 크다.
        """
        return await self._repository.owns_post(user_id, post_id)

    async def list_blog_tasks(self, user_id: str) -> list[BlogTask]:
        if not user_id.strip():
            raise BlogTaskError("VALIDATION_FAILED", "userId is required")
        return await self._repository.list_by_user_id(user_id.strip())

    async def list_blog_task_items(self, user_id: str) -> list[BlogTaskListItem]:
        """본문과 이미지를 제외한 내 글 목록 카드 데이터만 반환한다."""
        if not user_id.strip():
            raise BlogTaskError("VALIDATION_FAILED", "userId is required")
        return await self._repository.list_items_by_user_id(user_id.strip())

    async def delete_user_blog_task(self, user_id: str, post_id: str) -> None:
        """소유자와 post id가 둘 다 맞을 때만 지운다."""
        await self._repository.delete_by_user_and_post_id(user_id, post_id)

    async def publish_blog_task(self, post_id: str, raw_body: Any) -> BlogTask:
        method, channel = _validate_publish_request(raw_body)
        task = await self._require_task(post_id)

        retryable_statuses = {
            BlogTaskStatus.READY_TO_PUBLISH,
            BlogTaskStatus.POSTING_NEEDS_HUMAN,
            BlogTaskStatus.FAILED,
        }
        # POSTED라도 요청한 채널에 성공 기록이 없으면 발행할 수 있다 — 과거 복사 완료
        # 재발행과, 한 채널에 올린 글의 다른 채널 발행(예: 스레드 후 네이버)이 여기다.
        postable_from_posted = (
            task.status == BlogTaskStatus.POSTED
            and not _has_successful_auto_publish(task, channel)
        )
        if task.status not in retryable_statuses and not postable_from_posted:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"cannot publish blogTask {post_id} from {task.status.value}",
            )
        if task.final_post is None:
            raise BlogTaskError("VALIDATION_FAILED", f"blogTask {post_id} has no finalPost")

        # 스레드 발행이면 **스레드 문법으로 게시물을 새로 쓴다**(첫 줄 훅·짧은 문단·
        # 해시태그 없음). 블로그 원고를 문단 경계에서 잘라 담는 것은 생성기가 없을 때의
        # 폴백일 뿐이다(2026-08-06 사용자 요청으로 되살렸다).
        #
        # **상태 전이보다 먼저** 만든다. 생성이 실패해도 글은 READY_TO_PUBLISH에 그대로
        # 남아 다시 시도할 수 있다 — POSTING으로 옮겨 놓고 실패하면 발행도 안 된 글이
        # '발행 중'에 갇힌다.
        threads_texts = (
            await self._write_threads_post(task)
            if channel == PostingChannel.THREADS
            else None
        )

        # POSTING_NEEDS_HUMAN은 상태 머신에 재시도 경로가 있다. 발행 완료·실패 상태는
        # 원고를 보존한 채 READY_TO_PUBLISH로 되감은 후 자동 발행한다.
        if task.status in {BlogTaskStatus.POSTED, BlogTaskStatus.FAILED}:
            await self._repository.rewind_status(
                post_id, BlogTaskStatus.READY_TO_PUBLISH, POSTING_ACTOR
            )
        posting = await self._repository.transition_status(
            post_id, BlogTaskStatus.POSTING, POSTING_ACTOR
        )

        result = await self._run_posting_worker(
            PublishJob(
                post_id=post_id,
                user_id=posting.user_id,
                method=method,
                final_post=task.final_post,
                channel=channel,
                threads_texts=threads_texts,
                threads_topic=_threads_topic_for(task),
            )
        )

        await self._repository.append_posting_log(
            post_id,
            PostingLog(
                log_id=new_log_id(),
                post_id=post_id,
                user_id=posting.user_id,
                method=method,
                channel=channel,
                result=result.result,
                post_url=result.post_url,
                error_message=result.error_message,
                created_at=_now(),
            ),
        )

        if result.result == PostingResultStatus.SUCCESS and method == PostingMethod.DRAFT:
            # 네이버 임시저장은 공개 발행이 아니다. 원고를 다시 발행할 수 있도록
            # READY_TO_PUBLISH로 되돌리고, 성공 사실은 postingLogs에 남긴다.
            return await self._repository.rewind_status(
                post_id, BlogTaskStatus.READY_TO_PUBLISH, POSTING_ACTOR
            )

        next_status = {
            PostingResultStatus.SUCCESS: BlogTaskStatus.POSTED,
            PostingResultStatus.NEEDS_HUMAN: BlogTaskStatus.POSTING_NEEDS_HUMAN,
        }.get(result.result, BlogTaskStatus.FAILED)

        return await self._repository.transition_status(post_id, next_status, POSTING_ACTOR)

    async def start_intent_analysis(self, post_id: str) -> BlogTask:
        """글을 SEARCH_ANALYZING으로 옮기고 M3을 백그라운드 잡에 넘긴다.

        M3은 모델 시간으로 1분쯤이다 — 검색 절반과 요약 절반이 차례로. 요약이 검색이
        찾은 것을 읽기 때문이다. 클라이언트는 요청에서 기다리지 않고 글을 폴링한다.
        """
        task = await self._require_task(post_id)

        # M2가 이미 여기로 옮겼을 수 있다. 두 번 전이하지 않는다.
        analyzing = (
            task
            if task.status == BlogTaskStatus.SEARCH_ANALYZING
            else await self._repository.transition_status(
                post_id, BlogTaskStatus.SEARCH_ANALYZING, WEB_SEARCH_ACTOR
            )
        )

        # 같은 근거의 검증이 이미 돌고 있으면 잡을 하나 더 띄우지 않는다. 상태는 이미
        # SEARCH_ANALYZING이라 상태 검사로는 걸러지지 않고, 그대로 두면 새로고침 후
        # '다시 검증'을 누를 때마다 검색·요약이 한 벌씩 더 돌아 과금이 배로 늘어난다.
        # 진행 중인 잡의 결과는 폴링이 받아 가므로, 여기서는 현재 상태만 돌려주면 된다.
        #
        # 근거가 다르면(제목을 다시 골랐다) 중복이 아니다 — 돌고 있는 잡은 옛 제목의
        # 것이라 결과를 저장하지 않고 끝나므로(_run_intent_analysis_locked의 stale 검사),
        # 새 제목의 검증을 여기서 시작하지 않으면 아무도 시작하지 않는다.
        key = _analysis_key(analyzing)
        if self._m3_inflight.get(post_id) == key:
            logger.info("검증 요청 무시 | %s - 이미 진행 중입니다", short(post_id))
            return analyzing.model_copy(update={"progress": None})

        # **하는 일에 맞는 칸 이름을 쓴다**(2026-08-12 사용자 신고). 자료를 모으지 않는
        # 실행(여러 편·예약 글)에 '자료 검색'이라고 적으면, 화면이 하지도 않은 일을 했다고
        # 말한다 — 실제로 5초 만에 끝난 실행이 "자료를 찾아 읽는 중"으로 보였다.
        collects = _collects_sources_now(analyzing.input)
        reporter = ProgressReporter(
            self._repository,
            post_id,
            TaskPhase.SEARCH,
            steps=None if collects else DIRECTIONS_ONLY_STEPS,
            name=None if collects else DIRECTIONS_ONLY_PHASE_NAME,
        )
        await reporter.step(0)
        # 단계 이름 위에 '지금 무엇을 하는 중'을 얹는다(2026-08-07 사용자 요청) —
        # 자료 검색은 1분쯤 걸리는데 단계 이름만 있으면 화면이 아무 말도 하지 않는다.
        # 이 단계를 실제로 도는 모델은 Gemini다(GeminiResearchAnalyzer — 수집·정리 모두).
        # 사용자 지적(2026-08-07): Claude라고 적혀 있었는데 웹 검색은 Claude가 아니다.
        await reporter.detail(
            "Gemini가 검색 키워드로 웹 자료를 찾아 읽는 중이에요…"
            if collects
            else "Gemini가 소재와 제목을 보고 글의 방향 후보를 나누는 중이에요…"
        )

        self._m3_inflight[post_id] = key
        # 핸들을 글 단위로 붙잡아 둔다(원고 생성과 같은 이유). start와 등록 사이에
        # await가 없어야 한다 — 그 사이에 잡이 끝나면 지운 뒤에 다시 넣게 된다.
        self._m3_jobs[post_id] = self._jobs.start(
            self._run_intent_analysis(analyzing, reporter)
        )

        return analyzing.model_copy(update={"progress": None})

    async def analyze_intent_candidates(self, post_id: str) -> BlogTask:
        """시작하고 기다린다 — 테스트와 결과가 필요한 것들을 위해.

        **자기 글의 잡만 기다린다.** 예전에는 `_jobs.drain()`으로 이 서비스의 잡을 전부
        기다려서, 백그라운드로 돌려 둔 다른 글의 검증까지 함께 기다렸다.

        같은 글의 검증이 이미 돌고 있으면 그 잡을 기다린다 — 결과는 어차피 그것이 쓴다.
        핸들이 없으면 기다리지 않는다(이미 끝났고 결과는 DB에 있다).
        """
        started = await self.start_intent_analysis(post_id)
        job = self._m3_jobs.get(started.post_id)
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        return await self._repository.find_by_post_id(started.post_id)

    async def cancel_intent_analysis(self, post_id: str) -> bool:
        """돌고 있는 검증을 **실제로 멈춘다.** 멈출 것이 있었으면 True.

        사용자가 검증 화면에서 '제목 다시 고르기'를 누르면 그 검증의 결과는 어느 쪽이든
        쓰이지 않는다 — 제목을 바꾸면 근거가 달라져 다시 돌려야 하고, 같은 제목으로
        돌아와도 사용자가 다시 시작한다. 그런데 잡은 계속 돌면서 Google 검색과 LLM을
        끝까지 쓴다(2026-08-07 사용자 지적: "검증단계가 돌아가면 안되지").

        취소는 안전하다. 진행 중인 HTTP 호출은 취소로 끊기고, 임차는 ``async with``의
        빠져나감이 풀며(hold), 아직 저장된 것은 없다 — 결과는 마지막에 한 번 쓴다.
        글의 상태(SEARCH_ANALYZING)는 그대로 두는 것이 맞다: 제목은 이미 골라 둔 것이고,
        다음 '검증' 요청이 그 자리에서 새로 시작한다.

        **표식과 핸들을 먼저 지운다.** 그래야 취소된 잡의 ``finally``가 남의 것을
        건드리지 않고, 곧바로 이어지는 새 검증이 '이미 진행 중'으로 걸러지지 않는다.
        """
        job = self._m3_jobs.pop(post_id, None)
        self._m3_inflight.pop(post_id, None)
        if job is None or job.done():
            return False
        job.cancel()
        try:
            await job
        except (asyncio.CancelledError, Exception):  # noqa: B014 - 취소 결과는 보지 않는다
            pass
        logger.info("검증 중단 | %s - 사용자가 제목을 다시 고릅니다", short(post_id))
        return True

    async def _run_intent_analysis(
        self, analyzing: BlogTask, reporter: ProgressReporter
    ) -> None:
        """임차를 잡은 프로세스만 실제로 돌린다.

        _m3_inflight가 이 프로세스 안의 중복을 막고, 임차는 프로세스가 여럿일 때를 막는다 —
        둘 다 필요하다. 임차만 있으면 Redis 없는 배포에서 무력해지고(NoOpJobLease는 항상
        잡힌다), in-flight만 있으면 API 서버를 늘리는 순간 서버마다 한 벌씩 돈다.

        끝날 때 in-flight에서 반드시 뺀다 — 예외로 빠져나가면서 남겨 두면 그 글은 다시
        검증할 수 없게 된다. 그 사이 사용자가 제목을 다시 골라 새 잡이 표식을 덮어썼다면
        그것은 남의 표식이므로 지우지 않는다.
        """
        key = _analysis_key(analyzing)
        post_id = analyzing.post_id
        try:
            held = await hold(self._lease, lease_key(post_id, "m3"))
            # 임차를 남이 쥐고 있다 = 대개 옛 제목의 검증이 아직 도는 중이다. 그 잡은
            # 제목이 바뀐 것을 보고 결과를 버리므로, 여기서 그냥 물러나면 새 제목의
            # 검증은 아무도 돌리지 않는다. 임차가 풀릴 때까지 기다렸다가 돌린다.
            # 상한을 두는 이유는 좀비가 되지 않기 위해서다 — 임차 자체가 120초면
            # 만료되므로 이 상한에 닿는 일은 임차 갱신이 계속 성공할 때뿐이다.
            waited = 0.0
            while held is None and waited < self._M3_LEASE_WAIT_SECONDS:
                await asyncio.sleep(2.0)
                waited += 2.0
                latest = await self._repository.find_by_post_id(post_id)
                if latest is None or _analysis_key(latest) != key:
                    # 기다리는 동안 제목이 또 바뀌었다 — 그 재선택이 띄운 잡에 넘긴다.
                    return
                if latest.intent_validation_result is not None:
                    # 같은 근거의 검증을 다른 프로세스가 먼저 끝냈다. 다시 돌 이유가 없다.
                    return
                held = await hold(self._lease, lease_key(post_id, "m3"))
            if held is None:
                logger.warning(
                    "검증 건너뜀 | %s - 임차를 %d초 안에 얻지 못했습니다",
                    short(post_id),
                    int(self._M3_LEASE_WAIT_SECONDS),
                )
                return
            async with held:
                await self._run_intent_analysis_locked(analyzing, reporter)
        finally:
            if self._m3_inflight.get(post_id) == key:
                self._m3_inflight.pop(post_id, None)
                # 표식이 내 것일 때만 핸들도 지운다. 그 사이 새 검증이 시작됐다면 그
                # 핸들은 남의 것이라 건드리면 안 된다.
                self._m3_jobs.pop(post_id, None)

    async def _run_intent_analysis_locked(
        self, analyzing: BlogTask, reporter: ProgressReporter
    ) -> None:
        post_id = analyzing.post_id
        trace = perf.start_trace("m3-verify", post_id)

        async def stale() -> bool:
            """검색이 도는 동안 사용자가 제목을 다시 골랐는가.

            그랬다면 이 잡의 결과는 옛 제목의 것이라 저장하지 않는다 — 제목 재선택이
            지워 둔 검증 결과 자리를 옛 결과로 도로 채우면, 새 제목의 검증 팝업이 옛
            제목의 방향·자료를 보여주게 된다. 새 검증은 재선택 뒤의 '작성 전 검증'
            요청이 새 잡으로 돌린다.
            """
            latest = await self._repository.find_by_post_id(post_id)
            return latest is None or _analysis_key(latest) != _analysis_key(analyzing)

        analysis_input = analyzing.input
        if analyzing.trend_selection:
            analysis_input = analyzing.input.model_copy(
                update={"topic": analyzing.trend_selection.final_topic}
            )

        async def collected() -> None:
            """자료 수집이 끝나고 검증으로 넘어가는 시점. 단계와 함께 내레이션도 바꾼다.

            자료를 모으지 않는 실행에는 넘어갈 칸이 없다 — 그 실행은 한 칸짜리다.
            """
            if reporter.step_count < 2:
                return
            await reporter.step(1)
            await reporter.detail("Gemini가 모은 자료를 검증해 글의 방향 후보를 추리는 중이에요…")

        async def note(message: str) -> None:
            """수집기가 흘려보낸 한 줄을 그대로 작업 현황에 남긴다(2026-08-11).

            예전에는 '네이버 블로그 보강 3건'·'최신 기사 4건' 같은 줄이 서버 로거로만
            나가고, 화면은 1분 내내 같은 문장 하나만 보여 줬다.
            """
            await reporter.detail(message)

        try:
            result = await self._web_search_analyzer.search_and_analyze(
                WebSearchAnalysisInput(
                    post_id=analyzing.post_id,
                    user_id=analyzing.user_id,
                    input=analysis_input,
                    prompt_version=M3_PROMPT_VERSION,
                    # 사용자가 고른 검색 키워드로 자료를 모으게 한다 — 소재 제목만 주면
                    # 수집이 일반 상위 결과에 머문다(2026-08-04 사용자 요청).
                    selected_keywords=(
                        analyzing.trend_selection.selected_keywords
                        if analyzing.trend_selection
                        else []
                    ),
                    collect_sources=_collects_sources_now(analyzing.input),
                ),
                on_collected=collected,
                # 구형 어댑터·테스트 스텁은 이 인자를 모른다. 받는 쪽만 넘긴다 —
                # 진행 표시 하나 때문에 검증이 TypeError로 죽어서는 안 된다.
                **({"on_note": note} if _accepts_note(self._web_search_analyzer) else {}),
            )
        except Exception as error:
            # 글을 버리지 않는다. FAILED는 종착 상태라, 한 번 타임아웃한 모델이 입력과
            # 주제가 멀쩡한 글을 죽이곤 했다. 글은 SEARCH_ANALYZING에 남고, 다시 검증은
            # 거기서 다시 돈다.
            #
            # 다만 조용히 끝내지도 않는다. 아무것도 저장하지 않으면 검증 팝업은 '실패'와
            # '결과 없음'을 구분하지 못해 "검색 결과가 없습니다"만 반복해 보여주고, 다시
            # 검증을 눌러도 같은 화면이라 사용자가 이유도 모른 채 갇힌다. 실패 사유를 담은
            # 결과를 남겨 무엇이 잘못됐는지 보이게 하고, 자료 없이 진행할지 고를 수 있게 한다.
            logger.warning("검증(자료 검색) 실패 | %s - %s", short(post_id), error)
            try:
                if await stale():
                    # 진행 표시도 건드리지 않는다 — 새 제목의 잡이 이미 자기 것을 쓰고 있다.
                    logger.info(
                        "검증 실패 결과 폐기 | %s - 도는 사이 제목이 바뀌었습니다", short(post_id)
                    )
                    trace.finish()
                    return
                await self._repository.save_intent_validation_result(
                    post_id, _failed_validation_result(post_id, analysis_input, error)
                )
            except Exception as save_error:  # 저장 실패까지 검증을 죽이지는 않는다.
                logger.warning("검증 실패 사유 저장 실패 | %s - %s", short(post_id), save_error)
            await reporter.clear(ok=False)
            trace.finish()
            return

        if await stale():
            logger.info("검증 결과 폐기 | %s - 도는 사이 제목이 바뀌었습니다", short(post_id))
            trace.finish()
            return

        with perf.span("database_save"):
            await self._repository.save_intent_validation_result(post_id, result)
        await reporter.clear(ok=True)
        trace.finish()

    async def select_intent(self, post_id: str, raw_body: Any) -> BlogTask:
        parsed = validate_select_intent_request(raw_body)
        return await self._repository.select_intent(
            post_id, parsed.intent_id, INTENT_SELECTION_ACTOR, parsed.excluded_source_urls
        )

    async def stop_orphaned_generations(self) -> int:
        """서버가 꺼질 때 돌던 원고 생성을 **멈춤으로 표시한다**(2026-08-12 사용자 지시).

            "서버를 끄면 작업은 중지되게 만들어"

        GENERATING으로 적혀 있지만 **그것을 돌리는 프로세스는 이미 없다.** 그대로 두면
        두 가지가 잘못된다:

        - 내 글 목록이 '원고 만드는 중'이라고 거짓을 말한다. 며칠이 지나도 그대로다.
        - 그 글을 열면 화면이 도는 작업에 따라붙으려 하고(``running``), 아무도 돌리지
          않으므로 0%에서 멈춘 채 버튼도 없이 남는다.

        FAILED로 옮기면 화면이 **'다시 생성하기'**를 준다 — 사람이 눌러야 다시 돈다는
        것이 이 지시의 뜻이다.

        시작할 때 한 번만 부른다. 돌려주는 것은 멈춘 글 수다.
        """
        stuck = await self._repository.list_by_status([BlogTaskStatus.GENERATING])
        for task in stuck:
            await self._repository.transition_status(
                task.post_id, BlogTaskStatus.FAILED, "system:startup-stop"
            )
        return len(stuck)

    async def apply_round_pick(
        self,
        post_id: str,
        intent_id: str,
        *,
        final_topic: str | None = None,
        intent: IntentCandidate | None = None,
    ) -> BlogTask:
        """**1편째로 고른 제목·방향**을 원본 글에 적용한다(2026-08-12).

        여러 편을 만들 때 화면은 라운드마다 고른 것을 배열로 들고 있다가 마지막에 한 번에
        보낸다 — 라운드마다 저장하면 글 하나의 한 자리를 두고 서로 덮어쓰기 때문이다.
        그래서 마지막에, 첫 편은 원본 글에 이렇게 적용하고 나머지는 복제한다
        (``clone_for_direction``).

        제목은 주면 갈아 끼우고 없으면 그대로 둔다 — 제목 단계를 건너뛴 흐름이 있다.

        ``intent``를 주면 **그 방향을 글에 되돌려 놓은 뒤** 고른다(2026-08-12 사용자 신고).
        이 글에 남아 있는 후보는 **마지막 편의 것**이다 — 편마다 제목을 다시 고르면
        ``save_trend_selection``이 옛 검증 결과를 지우고 새 검증이 그 자리를 채우기 때문이다.
        그래서 1편째의 자리번호는 여기서 다른 방향을 가리키고 있거나 아예 없다.
        """
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")

        retitled = _retitled(task.trend_selection, final_topic, _now())
        if retitled is not None and retitled != task.trend_selection:
            task = await self._repository.save_trend_selection(
                post_id, retitled, INTENT_SELECTION_ACTOR
            )
        # 제목을 갈아 끼우면 검증 결과가 함께 지워진다(save_trend_selection). 그래서 방향을
        # 되돌려 놓는 것은 **그다음**이어야 한다 — 순서가 바뀌면 방금 넣은 것이 지워진다.
        if intent is not None:
            task = await self._repository.save_intent_validation_result(
                post_id, _with_chosen_candidate(task.intent_validation_result, intent)
            )
        already = task.selected_intent
        if (
            already is not None
            and already.intent_id == intent_id
            # 자리번호가 같아도 **다른 방향**일 수 있다. 방향을 함께 받았으면 제목까지 같아야
            # '이미 그것'이다 — 번호만 보고 넘기면 고르지도 않은 방향으로 원고가 나간다.
            and (intent is None or already.title == intent.title)
        ):
            # 이미 그 방향이다 — 다시 고르면 상태 전이가 막힌다(INTENT_SELECTED에서
            # 또 INTENT_SELECTED로는 갈 수 없다).
            return task
        return await self._repository.select_intent(
            post_id, intent_id, INTENT_SELECTION_ACTOR
        )

    async def clone_for_direction(
        self,
        post_id: str,
        intent_id: str,
        *,
        final_topic: str | None = None,
        intent: IntentCandidate | None = None,
    ) -> BlogTask:
        """같은 소재·같은 제목으로 **다른 방향의 글 하나**를 더 만든다(2026-08-12).

        한 소재로 여러 편을 만들 때 2편·3편이 여기서 태어난다. 원본이 그대로 1편이고,
        이 글은 방향만 다르다.

        **복사하는 것과 하지 않는 것**을 나눈 기준은 "사람이 정한 것인가"다:

        - 복사한다: 소재·목적·연령대·참고자료(``input``), 확정한 제목(``trend_selection``),
          검증 결과와 방향 후보(``intent_validation_result``). 사용자가 한 번 정한 것을
          편마다 다시 고르게 할 이유가 없다.
        - 복사하지 않는다: 원고·이미지·최종 글·진행 상태. 이 글은 아직 아무것도 만들지
          않았다. 복사하면 만들지도 않은 원고가 있는 것처럼 보인다.

        ``draft_count``는 **1로 되돌린다.** 그대로 두면 이 글이 또 여러 편을 부르는
        셈이 되어 끝없이 불어난다.

        ``final_topic``을 주면 **제목도 이 글의 것으로 갈아 끼운다**(2026-08-12). 편마다
        제목과 방향을 함께 고르는 흐름에서 쓴다 — 글 하나가 제목 하나와 방향 하나를 들고,
        원고는 그 둘로 만들어진다. 주지 않으면 원본의 제목을 그대로 쓴다(예약 경로의
        "같은 제목을 여러 각도로"가 그대로 남는다).
        """
        origin = await self._repository.find_by_post_id(post_id)
        if origin is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        # 방향을 함께 받았으면 원본에 그것이 남아 있는지 따지지 않는다(2026-08-12 사용자 신고).
        # 편마다 제목을 다시 고르면 원본에는 **마지막 편의 후보만** 남는다 — 앞 편의
        # 자리번호는 여기서 다른 방향을 가리키거나 아예 없다. 화면이 보낸 방향이 그 편이
        # 실제로 고른 것이므로, 그것을 이 복제본의 후보로 심는다.
        if intent is None:
            if origin.intent_validation_result is None:
                raise BlogTaskError(
                    "VALIDATION_FAILED", f"blogTask {post_id} has no intent candidates"
                )
            if not any(
                candidate.intent_id == intent_id
                for candidate in origin.intent_validation_result.intent_candidates
            ):
                raise BlogTaskError(
                    "VALIDATION_FAILED", f"intent {intent_id} is not a candidate of {post_id}"
                )

        now = _now()
        clone = BlogTask(
            post_id=new_post_id(),
            user_id=origin.user_id,
            version=1,
            # **원본의 상태를 물려받지 않는다**(2026-08-12). 원본은 이미 방향을 골라
            # INTENT_SELECTED인데, 거기서 다시 INTENT_SELECTED로는 갈 수 없어 바로 아래
            # select_intent가 INVALID_STATUS_TRANSITION으로 죽는다(실제로 500이 났다).
            #
            # 이 글이 서 있는 자리는 "검증은 끝났고 방향은 아직"이다 — 그게 곧
            # SEARCH_ANALYZING이고, 거기서만 방향 선택으로 넘어갈 수 있다.
            status=BlogTaskStatus.SEARCH_ANALYZING,
            created_at=now,
            updated_at=now,
            input=origin.input.model_copy(update={"draft_count": 1}),
            trend_selection=_retitled(origin.trend_selection, final_topic, now),
            intent_validation_result=(
                _with_chosen_candidate(origin.intent_validation_result, intent)
                if intent is not None
                else origin.intent_validation_result
            ),
        )
        await self._repository.create(clone)
        return await self._repository.select_intent(
            clone.post_id, intent_id, INTENT_SELECTION_ACTOR
        )

    async def refresh_selected_intent_sources(self, post_id: str) -> BlogTask:
        """고른 방향은 그대로 두고 **자료만 새로 모아** 갈아끼운다(2026-08-11 예약 경로).

        새 글 작성에서 방향까지 골라 둔 글이, 지정한 작업 시각에 원고를 만들기 직전에
        부른다. 방향(제목·독자·논지)은 사람의 판단이라 며칠 뒤에도 유효하지만 자료는
        낡는다 — 이 예약의 존재 이유가 바로 그 차이다.

        수집기가 이 기능을 지원하지 않거나(구형 어댑터·테스트 스텁) 수집이 실패하면 **글을
        그대로 둔다.** 자료를 새로 못 모았다고 약속한 시각의 발행을 포기하는 것보다, 옛
        자료로 쓰고 그 사실을 로그에 남기는 쪽이 낫다.
        """
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        if task.selected_intent is None:
            raise BlogTaskError(
                "VALIDATION_FAILED", f"blogTask {post_id} has no selected intent"
            )

        collect = getattr(self._web_search_analyzer, "collect_sources", None)
        if collect is None:
            logger.info("자료 재수집 미지원 수집기 — 옛 자료로 진행 | %s", short(post_id))
            return task

        analysis_input = task.input
        if task.trend_selection:
            analysis_input = task.input.model_copy(
                update={"topic": task.trend_selection.final_topic}
            )
        try:
            sources = await collect(
                WebSearchAnalysisInput(
                    post_id=task.post_id,
                    user_id=task.user_id,
                    input=analysis_input,
                    prompt_version=M3_PROMPT_VERSION,
                    selected_keywords=(
                        task.trend_selection.selected_keywords
                        if task.trend_selection
                        else []
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001 - 약속한 시각을 자료 때문에 놓치지 않는다
            logger.warning(
                "자료 재수집 실패 — 옛 자료로 진행합니다 | %s - %s", short(post_id), error
            )
            return task

        if not sources:
            logger.info("자료 재수집 결과 없음 — 옛 자료로 진행 | %s", short(post_id))
            return task
        logger.info("자료 재수집 완료 | %s - %d건", short(post_id), len(sources))
        return await self._repository.replace_selected_intent_sources(
            post_id, sources, INTENT_SELECTION_ACTOR
        )

    async def _begin_reference_processing(self, task: BlogTask) -> BlogTask:
        """글을 다음 단계로 옮기기만 한다. 여기서 모델은 호출하지 않는다.

        M1은 소재마다 Gemini를 한 번 호출하고 그 답을 버리곤 했다 — 아무도 읽지 않았다.
        그래서 소재를 저장하는 일이 아무 결과도 없이 모델을 기다렸고, 모델이 처리하지 못한
        참고 URL 하나가 글 전체를 끌고 내려갔다: REFERENCE_PROCESSING -> FAILED, 그리고
        제목 단계는 409로 답했다.

        URL이 URL인지는 검증에서 확인하고, 여기엔 모델이 필요 없다. 그 안에 무엇이 있는지는
        M3의 몫이다: 검증 단계가 사용자의 참고 URL을 먼저 검색한다.
        """
        return await self._repository.transition_status(
            task.post_id, BlogTaskStatus.REFERENCE_PROCESSING, REFERENCE_INTAKE_ACTOR
        )

    async def _article_length_for(self, user_id: str) -> str | None:
        """스레드 개수를 정하는 글 길이 설정. 못 읽으면 None(생성기가 중간으로 폴백)."""
        if self._user_settings is None:
            return None
        try:
            settings = await self._user_settings.get_by_user_id(user_id)
        except Exception as error:
            logger.warning("스레드 길이 설정 조회 실패 | %s - %s", user_id, error)
            return None
        return getattr(settings, "article_length", None) if settings else None

    async def _write_threads_post(self, task: BlogTask) -> list[str] | None:
        """스레드 전용 게시물 목록. 생성기가 없으면 None(발행기가 원고를 잘라 담는다).

        소재 하나를 **스레드 문법으로 새로 써서** 여러 스레드로 나눠 돌려준다. 개수는
        글 길이 설정이 정한다 — 모델이 스스로 고르지 않는다.

        생성 실패는 조용히 폴백하지 않는다 — 사용자가 스레드 문법의 글을 기대했는데
        블로그를 자른 글이 나가면 그게 더 나쁜 결과다. 상태 전이 전이므로 글은 그대로
        남고, 오류 메시지가 화면 토스트로 올라간다.
        """
        write = getattr(self._threads_writer, "generate_threads_post", None)
        if write is None:
            return None
        article_length = await self._article_length_for(task.user_id)
        try:
            return await write(task, article_length=article_length)
        except Exception as error:
            logger.warning("스레드 게시물 생성 실패 | %s - %s", task.post_id, error)
            raise BlogTaskError(
                "THREADS_DRAFT_FAILED",
                f"스레드 게시물 생성에 실패했습니다: {_failure_reason(error)}",
            ) from error

    async def _run_posting_worker(self, job: PublishJob) -> PublishResult:
        try:
            return await self._posting_worker.publish(job)
        except Exception as error:
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message=str(error) or "posting worker failed",
            )
