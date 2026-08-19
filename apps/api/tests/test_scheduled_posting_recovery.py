"""서버 재시작 복구(recover_active_batches·_stage_for).

프로세스가 죽는 시점은 고를 수 없다. 저장된 ``ScheduledJob.stage``는 '어디까지 갔다고
우리가 적어 둔 것'일 뿐이고, 실제로 무엇이 끝났는지는 연결된 BlogTask에 있다 — 그래서
복구는 두 기록을 맞춰 본다.

여기서 가장 중요한 규칙 하나: **결과가 불확실한 네이버 발행을 자동으로 다시 하지 않는다.**
이미 올라간 글을 다시 올리면 같은 글이 두 번 게시되고 되돌릴 수 없다. 그 규칙을 지키는
테스트가 `test_발행_도중_죽은_작업은_자동으로_다시_발행하지_않는다`다.

LLM·Mongo·셀레니움은 하나도 부르지 않는다 — 메모리 저장소와 손으로 만든 가짜 서비스뿐이다.
"""

from app.modules.scheduled_posting.models import (
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStage,
    ScheduledJobStatus,
)
from app.modules.scheduled_posting.recovery import _stage_for, recover_active_batches
from app.modules.scheduled_posting.repository import InMemoryScheduledPostingRepository
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    FinalPost,
    IntentCandidate,
    IntentValidationResult,
    PostingChannel,
    PostingLog,
    PostingMethod,
    PostingResultStatus,
    SelectedIntent,
    TrendSelection,
)

#: 죽기 전에 적힌 시각. 복구가 손댄 문서는 updated_at이 이 값에서 바뀐다.
옛날 = "2026-08-03T09:00:00.000Z"


class FakeBlogTaskService:
    """복구가 실제로 쓰는 것은 ``get_blog_task`` 하나뿐이다.

    나머지 메서드는 일부러 터지게 둔다 — 복구가 글을 만들거나 원고를 쓰거나 발행을
    다시 부르면 그 자리에서 실패해야 한다.
    """

    def __init__(self, tasks=()):
        self._tasks = {task.post_id: task for task in tasks}
        #: 어떤 글을 물어봤는지. '아예 묻지 않았다'를 확인하는 데 쓴다.
        self.asked: list[str] = []

    async def get_blog_task(self, post_id):
        self.asked.append(post_id)
        return self._tasks.get(post_id)

    async def create_blog_task(self, body):
        raise AssertionError("복구가 글을 새로 만들면 안 됩니다.")

    async def publish_blog_task(self, post_id, body):
        raise AssertionError("복구가 네이버 발행을 다시 부르면 안 됩니다.")


def build_task(**overrides) -> BlogTask:
    """소재 하나짜리 BlogTask. 필요한 필드만 바꿔 끼운다."""
    base = {
        "post_id": "post_1",
        "user_id": "user_1",
        "status": BlogTaskStatus.REFERENCE_PROCESSING,
        "version": 1,
        "created_at": 옛날,
        "updated_at": 옛날,
        "input": BlogTaskInput(topic="가을 등산", keywords=["등산"]),
    }
    return BlogTask(**{**base, **overrides})


def build_job(**overrides) -> ScheduledJob:
    base = {
        "job_id": "job_1",
        "batch_id": "batch_1",
        "user_id": "user_1",
        "sequence": 0,
        "topic": "가을 등산",
        "created_at": 옛날,
        "updated_at": 옛날,
    }
    return ScheduledJob(**{**base, **overrides})


def build_batch(**overrides) -> ScheduledBatch:
    base = {
        "batch_id": "batch_1",
        "user_id": "user_1",
        "status": ScheduledBatchStatus.RUNNING,
        "target_count": 1,
        "interval_seconds": 1800,
        "total_count": 1,
        "created_at": 옛날,
        "updated_at": 옛날,
    }
    return ScheduledBatch(**{**base, **overrides})


def 네이버_성공_로그(post_url="https://blog.naver.com/user/1") -> PostingLog:
    """이 글이 네이버에 실제로 올라갔다는 기록."""
    return PostingLog(
        log_id="log_1",
        post_id="post_1",
        user_id="user_1",
        method=PostingMethod.AUTO,
        channel=PostingChannel.NAVER,
        result=PostingResultStatus.SUCCESS,
        post_url=post_url,
        created_at=옛날,
    )


def 스레드_성공_로그(post_url="https://www.threads.com/@user/post/1") -> PostingLog:
    """이 글이 쓰레드에 실제로 올라갔다는 기록. 쓰레드 단독 예약에는 이것뿐이다."""
    return PostingLog(
        log_id="log_2",
        post_id="post_1",
        user_id="user_1",
        method=PostingMethod.AUTO,
        channel=PostingChannel.THREADS,
        result=PostingResultStatus.SUCCESS,
        post_url=post_url,
        created_at=옛날,
    )


def final_post() -> FinalPost:
    return FinalPost(
        title="가을 등산 준비물",
        body="본문",
        hashtags=["등산"],
        html_content="<h1>가을 등산 준비물</h1>",
    )


async def run_recovery(jobs, tasks=(), batch=None):
    """배치 하나를 만들어 두고 복구를 돌린다. (저장소, 가짜 서비스, 되살린 수)"""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(batch or build_batch(), list(jobs))
    service = FakeBlogTaskService(tasks)
    touched = await recover_active_batches(repository, service)
    return repository, service, touched


# ------------------------------------------------------- 글을 아직 만들지 않은 작업


async def test_글을_만들기_전에_죽은_작업은_처음부터_다시_대기한다():
    """postId가 없으면 남은 것이 아무것도 없다 — 되돌릴 것도 없이 처음부터다."""
    repository, service, touched = await run_recovery(
        [
            build_job(
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.DRAFT_GENERATION,
            )
        ]
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.WAITING
    assert job.stage == ScheduledJobStage.CREATE_POST
    assert job.updated_at != 옛날
    assert touched == 1
    # 가리키는 글이 없으니 BlogTask를 물어볼 일도 없다.
    assert service.asked == []


async def test_처음_단계에서_대기_중이던_작업은_손대지_않는다():
    """이미 맞게 적혀 있는 문서를 다시 쓰지 않는다 — 되살린 수에도 세지 않는다."""
    repository, _, touched = await run_recovery(
        [build_job(status=ScheduledJobStatus.WAITING)]
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.WAITING
    assert job.updated_at == 옛날
    assert touched == 0


# ------------------------------------------------------------------ 글이 사라짐


async def test_postId는_있는데_글이_사라졌으면_postId를_버리고_처음부터_한다():
    """사용자가 글을 지웠을 수 있다. 지어낸 글로 발행하지 않는다."""
    repository, service, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.DRAFT_GENERATION,
            )
        ],
        tasks=(),  # 그 글이 없다
    )

    job = await repository.find_job("job_1")
    assert job.post_id is None
    assert job.status == ScheduledJobStatus.WAITING
    assert job.stage == ScheduledJobStage.CREATE_POST
    assert service.asked == ["post_1"]
    assert touched == 1


# ----------------------------------------------------------- 이미 올라간 글


async def test_네이버_발행_성공_기록이_있으면_무슨_상태로_적혀_있든_완료로_맞춘다():
    """성공 로그가 다른 모든 판단보다 앞선다.

    글은 POSTING에 멈춰 있고 작업은 발행 중으로 적혀 있어도, 네이버에는 이미 올라갔다.
    이 확인이 '발행 결과 불명'보다 먼저 오지 않으면, 실제로 성공한 글이 실패로 남는다.
    """
    for 적힌_상태 in (
        ScheduledJobStatus.RUNNING,
        ScheduledJobStatus.PUBLISHING,
        ScheduledJobStatus.READY_TO_PUBLISH,
    ):
        repository, _, touched = await run_recovery(
            [
                build_job(
                    post_id="post_1",
                    status=적힌_상태,
                    stage=ScheduledJobStage.NAVER_PUBLISH,
                )
            ],
            tasks=[
                build_task(
                    status=BlogTaskStatus.POSTING,
                    posting_logs=[네이버_성공_로그()],
                )
            ],
        )

        job = await repository.find_job("job_1")
        assert job.status == ScheduledJobStatus.COMPLETED
        assert job.stage == ScheduledJobStage.DONE
        assert job.post_url == "https://blog.naver.com/user/1"
        assert job.published_at is not None
        assert touched == 1


async def test_쓰레드_단독_예약은_쓰레드_성공_기록으로_완료를_판단한다():
    """2026-08-06 — 그 작업에는 네이버 로그가 영영 생기지 않는다.

    네이버 기준으로 보면 이미 올라간 글이 매번 '미완료'로 되살아나, 재시작할 때마다
    같은 글이 스레드에 한 번 더 올라간다.
    """
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                publish_naver=False,
                publish_threads=True,
                status=ScheduledJobStatus.PUBLISHING,
                stage=ScheduledJobStage.THREADS_PUBLISH,
            )
        ],
        tasks=[build_task(status=BlogTaskStatus.POSTING, posting_logs=[스레드_성공_로그()])],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.stage == ScheduledJobStage.DONE
    assert job.post_url == "https://www.threads.com/@user/post/1"
    assert touched == 1


async def test_쓰레드_단독_예약이_발행_중_죽으면_쓰레드를_확인하라고_남긴다():
    """자동으로 다시 올리지 않는다 — 확인할 곳은 네이버가 아니라 쓰레드다."""
    repository, _, _ = await run_recovery(
        [
            build_job(
                post_id="post_1",
                publish_naver=False,
                publish_threads=True,
                status=ScheduledJobStatus.PUBLISHING,
                stage=ScheduledJobStage.THREADS_PUBLISH,
            )
        ],
        tasks=[build_task(status=BlogTaskStatus.POSTING)],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "PUBLISH_RESULT_UNKNOWN"
    assert "쓰레드" in job.error_message


async def test_성공_로그에_주소가_없어도_이미_적힌_주소를_지우지_않는다():
    repository, _, _ = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.PUBLISHING,
                post_url="https://blog.naver.com/user/기존",
            )
        ],
        tasks=[
            build_task(
                status=BlogTaskStatus.POSTED,
                posting_logs=[네이버_성공_로그(post_url=None)],
            )
        ],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.post_url == "https://blog.naver.com/user/기존"


# ------------------------------------------------------- 발행 결과를 모르는 작업


async def test_발행_도중_죽은_작업은_자동으로_다시_발행하지_않는다():
    """이 파일에서 가장 중요한 규칙이다.

    발행 버튼을 누르던 중에 프로세스가 죽으면 네이버에 올라갔는지 알 수 없다. 여기서
    작업을 WAITING으로 되돌리면 워커가 곧바로 집어 들어 **같은 글을 두 번 게시**한다.
    그래서 실패로 남기고 사람이 네이버를 확인한 뒤 재시도를 누르게 한다.
    """
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.PUBLISHING,
                stage=ScheduledJobStage.NAVER_PUBLISH,
            )
        ],
        tasks=[
            # 원고는 다 있고 성공 로그만 없다 — 올라갔는지 안 갔는지 모르는 그 창이다.
            build_task(status=BlogTaskStatus.READY_TO_PUBLISH, final_post=final_post())
        ],
    )

    job = await repository.find_job("job_1")
    # 워커가 집어 갈 수 있는 상태로 두지 않는 것이 핵심이다.
    assert job.status != ScheduledJobStatus.WAITING
    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "PUBLISH_RESULT_UNKNOWN"
    # 사람이 무엇을 해야 하는지 문장에 있어야 한다.
    assert "네이버" in job.error_message
    # 원고는 그대로 남는다 — 재시도는 발행만 다시 한다.
    assert job.post_id == "post_1"
    assert job.stage == ScheduledJobStage.NAVER_PUBLISH
    assert touched == 1


async def test_글이_POSTING에_멈춰_있으면_작업_상태와_무관하게_사람_확인으로_남긴다():
    """작업에는 발행 중이라고 적히기 전에 죽었어도, 글이 POSTING이면 같은 창이다."""
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.NAVER_PUBLISH,
            )
        ],
        tasks=[build_task(status=BlogTaskStatus.POSTING, final_post=final_post())],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "PUBLISH_RESULT_UNKNOWN"
    assert touched == 1


# --------------------------------------------------------------- 추가 인증 필요


async def test_네이버가_추가_인증을_요구한_채_죽었으면_작업도_배치도_사람을_기다린다():
    """캡차·2단계 인증은 실패가 아니다. 사용자가 인증을 마치고 재개를 누르면 이어 간다."""
    # 죽은 프로세스가 붙잡고 있던 '현재 작업' 표시가 없는 배치다.
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.NAVER_PUBLISH,
            )
        ],
        tasks=[
            build_task(
                status=BlogTaskStatus.POSTING_NEEDS_HUMAN, final_post=final_post()
            )
        ],
        batch=build_batch(current_job_id=None),
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.NEEDS_HUMAN
    assert job.error_code == "NAVER_NEEDS_HUMAN"

    batch = await repository.find_batch("batch_1")
    assert batch.status == ScheduledBatchStatus.NEEDS_HUMAN
    assert touched == 1


# ------------------------------------------------------------- 원고가 남은 작업


async def test_원고가_있는_작업은_원고를_지키되_발행을_잇지_않는다():
    """2026-08-12 사용자 지시: "서버가 다시 켜진다고 해서 이어서 진행하고 그런 거 없어".

    두 가지를 함께 지킨다:
    - **원고는 그대로 둔다.** finalPost가 있으면 M1~M4를 다시 돌릴 이유가 없다(모델
      호출과 이미지 비용이다). stage도 발행 단계 그대로다.
    - **자동으로 올리지 않는다.** 예전에는 READY_TO_PUBLISH로 세워 워커가 곧바로
      집어 올렸다 — 사용자가 모르는 사이에 글이 올라간다.
    """
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.DRAFT_GENERATION,
            )
        ],
        tasks=[
            build_task(status=BlogTaskStatus.READY_TO_PUBLISH, final_post=final_post())
        ],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "STOPPED_BY_RESTART"
    # 원고는 그대로다 — 발행 단계에 세워 두고, 같은 글을 쓴다.
    assert job.stage == ScheduledJobStage.NAVER_PUBLISH
    assert job.post_id == "post_1"
    assert job.generated_at is not None
    assert touched == 1


async def test_원고를_만들던_중_재시작하면_되살리지_않고_실패로_세운다():
    """2026-08-12 사용자 지시: "예약작업도 멈추는 게 맞아".

    예전에는 여기서 WAITING으로 되돌려 워커가 곧바로 다시 집어 갔다. 그러면 사용자가
    모르는 사이에 원고 생성이 다시 돈다. 실패로 세워 두면 작업 큐에서 보이고, 사람이
    재시도를 눌러야 시작한다.
    """
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.RUNNING,
                stage=ScheduledJobStage.CREATE_POST,
            )
        ],
        tasks=[
            build_task(
                status=BlogTaskStatus.INTENT_SELECTED,
                selected_intent=SelectedIntent(
                    intent_id="intent_1",
                    title="가을 등산 준비물",
                    target_reader="초보 등산객",
                    rationale="근거",
                ),
            )
        ],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "STOPPED_BY_RESTART"
    # 무엇 때문에 멈췄는지 작업 큐에서 읽혀야 한다.
    assert "재시작" in job.error_message
    assert touched == 1


# --------------------------------------------------------------- 이미 끝난 작업


async def test_이미_끝났거나_사람을_기다리는_작업은_건드리지_않는다():
    """되살릴 것이 아니다. COMPLETED를 다시 대기로 돌리면 같은 글이 두 번 올라간다."""
    끝난_상태들 = (
        ScheduledJobStatus.COMPLETED,
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.CANCELED,
        # 사람을 기다리는 작업도 마찬가지다 — 재개를 누르는 것은 사용자다.
        ScheduledJobStatus.NEEDS_HUMAN,
    )
    repository, service, touched = await run_recovery(
        [
            build_job(
                job_id=f"job_{index}",
                sequence=index,
                post_id="post_1",
                status=상태,
                stage=ScheduledJobStage.NAVER_PUBLISH,
            )
            for index, 상태 in enumerate(끝난_상태들)
        ],
        tasks=[build_task(status=BlogTaskStatus.POSTED)],
    )

    for index, 상태 in enumerate(끝난_상태들):
        job = await repository.find_job(f"job_{index}")
        assert job.status == 상태
        assert job.stage == ScheduledJobStage.NAVER_PUBLISH
        assert job.updated_at == 옛날
    assert touched == 0
    # 손댈 생각이 없으니 BlogTask를 읽지도 않는다.
    assert service.asked == []


# ------------------------------------------------------------------ 배치 정리


async def test_배치의_현재_작업_표시는_재시작_뒤_지워진다():
    """죽은 프로세스가 붙잡고 있던 표시다. 남겨 두면 워커가 영영 '누가 돌리는 중'이라고
    보고 다음 작업을 시작하지 않는다."""
    repository, _, touched = await run_recovery(
        [build_job(status=ScheduledJobStatus.WAITING)],
        batch=build_batch(current_job_id="job_1"),
    )

    batch = await repository.find_batch("batch_1")
    assert batch.current_job_id is None
    assert batch.updated_at != 옛날
    # 배치를 정리한 것은 되살린 '작업' 수에 세지 않는다.
    assert touched == 0


async def test_되살린_작업_수를_돌려준다():
    repository, _, touched = await run_recovery(
        [
            build_job(job_id="job_0", sequence=0, status=ScheduledJobStatus.RUNNING),
            build_job(job_id="job_1", sequence=1, status=ScheduledJobStatus.WAITING),
            build_job(
                job_id="job_2",
                sequence=2,
                status=ScheduledJobStatus.PUBLISHING,
                post_id="post_1",
            ),
            build_job(job_id="job_3", sequence=3, status=ScheduledJobStatus.COMPLETED),
        ],
        tasks=[
            build_task(status=BlogTaskStatus.READY_TO_PUBLISH, final_post=final_post())
        ],
    )

    # 되살린 것은 RUNNING(→대기)과 PUBLISHING(→사람 확인) 둘뿐이다.
    assert touched == 2
    assert (await repository.find_job("job_1")).updated_at == 옛날
    assert (await repository.find_job("job_3")).updated_at == 옛날


# -------------------------------------------------------------- 단계 판정


def test_stage_for는_원고가_있으면_발행_단계다():
    """가장 앞선 사실이 이긴다 — 원고가 있으면 앞 단계 기록이 다 있어도 발행부터다."""
    task = build_task(
        status=BlogTaskStatus.READY_TO_PUBLISH,
        final_post=final_post(),
        selected_intent=SelectedIntent(
            intent_id="intent_1", title="제목", target_reader="독자", rationale="근거"
        ),
        trend_selection=TrendSelection(
            final_topic="가을 등산 준비물",
            selected_trend_keyword_ids=["trend_1"],
            skipped=False,
            selected_at=옛날,
        ),
    )

    assert _stage_for(task) == ScheduledJobStage.NAVER_PUBLISH


def test_stage_for는_의도를_골랐으면_원고_생성_단계다():
    task = build_task(
        status=BlogTaskStatus.INTENT_SELECTED,
        selected_intent=SelectedIntent(
            intent_id="intent_1", title="제목", target_reader="독자", rationale="근거"
        ),
    )

    assert _stage_for(task) == ScheduledJobStage.DRAFT_GENERATION


def test_stage_for는_검증만_끝났으면_의도_선택_단계다():
    task = build_task(
        status=BlogTaskStatus.SEARCH_ANALYZING,
        intent_validation_result=IntentValidationResult(
            prompt_version="m3-intent@v1.0",
            provider="mock",
            model="mock",
            analyzed_at=옛날,
            intent_candidates=[
                IntentCandidate(
                    intent_id="intent_1",
                    title="제목",
                    target_reader="독자",
                    rationale="근거",
                    keywords=["등산"],
                    sources=[],
                )
            ],
        ),
    )

    assert _stage_for(task) == ScheduledJobStage.INTENT_SELECTION


def test_stage_for는_제목만_골랐으면_자료_분석_단계다():
    task = build_task(
        status=BlogTaskStatus.SEARCH_ANALYZING,
        trend_selection=TrendSelection(
            final_topic="가을 등산 준비물",
            selected_trend_keyword_ids=["trend_1"],
            skipped=False,
            selected_at=옛날,
        ),
    )

    assert _stage_for(task) == ScheduledJobStage.SEARCH_ANALYSIS


def test_stage_for는_글만_만들어_뒀으면_트렌드_추천_단계다():
    assert (
        _stage_for(build_task(status=BlogTaskStatus.REFERENCE_PROCESSING))
        == ScheduledJobStage.TREND_RECOMMENDATION
    )


def test_stage_for는_아무_기록도_없으면_글_생성_단계다():
    """실패했거나 상태만 남은 글도 여기로 떨어진다 — 지어낸 단계를 만들지 않는다."""
    for 상태 in (BlogTaskStatus.INPUT, BlogTaskStatus.FAILED):
        assert _stage_for(build_task(status=상태)) == ScheduledJobStage.CREATE_POST


# ------------------------------------------------- 회귀: 복구가 배치 상태를 잃던 문제


async def test_인증_대기로_바뀐_배치_상태를_현재작업_정리가_지우지_않는다():
    """복구 도중 배치를 두 번 쓰는데, 두 번째가 첫 번째를 덮던 버그의 회귀 테스트.

    `recover_active_batches`는 작업을 하나씩 되살린 뒤 배치의 '현재 작업' 표시를
    지운다. 그때 루프 진입 전에 읽어 둔 낡은 사본으로 저장하면, 그 사이 작업 복구가
    적어 둔 NEEDS_HUMAN이 사라진다(save_batch는 통째 교체다).

    상태가 사라지면 워커가 이 배치를 끝난 것으로 닫아 버리고, 사용자가 네이버 인증을
    마치고 '재개'를 눌러도 재개할 대상이 없다. **작업이 도는 동안 current_job_id는 늘
    채워져 있으므로 이것이 크래시의 기본 경로다.**
    """
    repository, _, _ = await run_recovery(
        [build_job(status=ScheduledJobStatus.RUNNING, post_id="post_1")],
        tasks=[build_task(status=BlogTaskStatus.POSTING_NEEDS_HUMAN, final_post=final_post())],
        batch=build_batch(current_job_id="job_1"),
    )

    job = await repository.find_job("job_1")
    batch = await repository.find_batch("batch_1")
    assert job.status == ScheduledJobStatus.NEEDS_HUMAN
    # 여기가 핵심 — 예전에는 RUNNING으로 되돌아갔다.
    assert batch.status == ScheduledBatchStatus.NEEDS_HUMAN
    assert batch.current_job_id is None


async def test_복구가_작업_상태로_진행_개수를_다시_센다():
    """발행 직후에 죽으면 그 작업은 카운터에 한 번도 세어지지 않는다.

    저장된 개수를 믿지 않고 작업 상태에서 다시 세야, 재시작 뒤 화면의 진행률이 실제와
    맞는다.
    """
    repository, _, _ = await run_recovery(
        [
            build_job(job_id="job_1", sequence=0, status=ScheduledJobStatus.RUNNING, post_id="post_1"),
            build_job(job_id="job_2", sequence=1, status=ScheduledJobStatus.CANCELED),
        ],
        tasks=[
            build_task(
                status=BlogTaskStatus.POSTED,
                final_post=final_post(),
                posting_logs=[네이버_성공_로그()],
            )
        ],
        batch=build_batch(total_count=2, current_job_id="job_1"),
    )

    batch = await repository.find_batch("batch_1")
    # 발행에 성공한 채 죽은 작업 1건 + 취소 1건이 개수에 반영된다.
    assert batch.completed_count == 1
    assert batch.canceled_count == 1
    assert batch.failed_count == 0


async def test_아직_시작하지_않은_예약은_재시작을_견딘다():
    """멈추는 것은 **작업 중이던 것**뿐이다(2026-08-12).

    시각이 오지 않아 기다리던 예약까지 실패로 만들면, 서버를 한 번 재시작했다는 이유로
    사용자가 걸어 둔 예약이 통째로 날아간다.
    """
    repository, _, touched = await run_recovery(
        [
            build_job(
                post_id="post_1",
                status=ScheduledJobStatus.WAITING,
                stage=ScheduledJobStage.DRAFT_GENERATION,
            )
        ],
        tasks=[
            build_task(
                status=BlogTaskStatus.INTENT_SELECTED,
                selected_intent=SelectedIntent(
                    intent_id="intent_1",
                    title="가을 등산 준비물",
                    target_reader="초보 등산객",
                    rationale="근거",
                ),
            )
        ],
    )

    job = await repository.find_job("job_1")
    assert job.status == ScheduledJobStatus.WAITING
