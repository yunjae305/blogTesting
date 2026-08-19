"""서버 재시작 복구.

저장된 ``ScheduledJob.stage``만 믿지 않는다. 그 값은 '어디까지 갔다고 우리가 적어 둔
것'이고, 실제로 무엇이 끝났는지는 **연결된 BlogTask**에 있다. 프로세스가 죽는 시점은
고를 수 없으므로, 적어 두기 직전에 죽었을 수 있다.

가장 중요한 규칙: **결과가 불확실한 네이버 발행을 자동으로 다시 하지 않는다.**
네이버에 이미 올라갔는데 다시 올리면 같은 글이 두 번 게시되고, 그것은 되돌릴 수 없다.
발행을 누르던 중에 죽은 작업은 사람이 확인하도록 남긴다.
"""

import logging

from app.shared import BlogTaskStatus
from app.shared.format import now_iso
from app.shared.ids import short

from .models import (
    ACTIVE_BATCH_STATUSES,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStage,
    ScheduledJobStatus,
)
from .repository import ScheduledPostingRepository
from .service import _successful_naver_log, _successful_threads_log

logger = logging.getLogger(__name__)


async def recover_active_batches(
    repository: ScheduledPostingRepository, blog_task_service
) -> int:
    """시작 시 한 번. 되살린 작업 수를 돌려준다."""
    batches = await repository.list_batches_by_status(list(ACTIVE_BATCH_STATUSES))
    touched = 0
    for batch in batches:
        jobs = await repository.list_jobs(batch.batch_id)
        for job in jobs:
            if await _recover_job(repository, blog_task_service, job):
                touched += 1

        # 배치를 **여기서 다시 읽는다.** 위의 작업 복구가 배치를 NEEDS_HUMAN으로 바꿨을
        # 수 있는데, 루프 진입 전에 읽은 사본으로 덮어쓰면 그 상태가 사라진다(save_batch는
        # 통째 교체다). 그러면 인증을 마친 사용자가 '재개'를 눌러도 거부된다.
        fresh = await repository.find_batch(batch.batch_id) or batch
        recovered = await repository.list_jobs(batch.batch_id)
        # 개수는 저장된 값을 믿지 않고 작업 상태에서 다시 센다 — 발행 직후에 죽으면
        # 그 작업은 카운터에 한 번도 세어지지 않아 진행률이 실제보다 낮게 남는다.
        completed = sum(1 for job in recovered if job.status == ScheduledJobStatus.COMPLETED)
        failed = sum(1 for job in recovered if job.status == ScheduledJobStatus.FAILED)
        canceled = sum(1 for job in recovered if job.status == ScheduledJobStatus.CANCELED)
        if (
            fresh.current_job_id is not None
            or fresh.completed_count != completed
            or fresh.failed_count != failed
            or fresh.canceled_count != canceled
        ):
            await repository.save_batch(
                fresh.model_copy(
                    update={
                        # 죽은 프로세스가 붙잡고 있던 '현재 작업' 표시를 지운다. 남겨 두면
                        # 워커가 영영 '누가 돌리는 중'이라고 보고 다음 작업을 시작하지 않는다.
                        "current_job_id": None,
                        "completed_count": completed,
                        "failed_count": failed,
                        "canceled_count": canceled,
                        "updated_at": now_iso(),
                    }
                )
            )
    if touched:
        logger.info("예약 작업 복구 | %d건", touched)
    return touched


async def _recover_job(
    repository: ScheduledPostingRepository, blog_task_service, job: ScheduledJob
) -> bool:
    # 이미 끝난 작업은 손대지 않는다.
    if job.status in {
        ScheduledJobStatus.COMPLETED,
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.CANCELED,
        ScheduledJobStatus.NEEDS_HUMAN,
    }:
        return False

    now = now_iso()

    # 아직 글도 만들지 않았다 — 처음부터 하면 된다.
    if not job.post_id:
        if job.status == ScheduledJobStatus.WAITING:
            return False
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.WAITING,
                    "stage": ScheduledJobStage.CREATE_POST,
                    "updated_at": now,
                }
            )
        )
        return True

    task = await blog_task_service.get_blog_task(job.post_id)
    if task is None:
        # 글이 사라졌다. postId를 버리고 처음부터 — 지어낸 글로 발행하지 않는다.
        await repository.save_job(
            job.model_copy(
                update={
                    "post_id": None,
                    "status": ScheduledJobStatus.WAITING,
                    "stage": ScheduledJobStage.CREATE_POST,
                    "updated_at": now,
                }
            )
        )
        return True

    # 고른 곳에 이미 성공적으로 올라갔다 — 무슨 상태로 적혀 있든 완료다.
    #
    # **어느 로그를 보는지가 작업마다 다르다.** 쓰레드 단독 예약(publish_naver=False)에는
    # 네이버 로그가 영영 생기지 않으므로, 그 기준으로 보면 이미 발행된 글이 매번
    # '미완료'로 되살아나 같은 글이 스레드에 두 번 올라간다(2026-08-06).
    success = (
        _successful_naver_log(task) if job.publish_naver else _successful_threads_log(task)
    )
    if success is not None:
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.COMPLETED,
                    "stage": ScheduledJobStage.DONE,
                    "post_url": success.post_url or job.post_url,
                    "published_at": job.published_at or now,
                    "updated_at": now,
                }
            )
        )
        return True

    # 발행을 누르던 중에 죽었다. 올라갔는지 알 수 없으므로 **자동으로 다시 올리지
    # 않는다.** 사람이 그 플랫폼을 확인하고 재시도를 누르게 남긴다.
    where = "네이버 블로그" if job.publish_naver else "쓰레드"
    if job.status == ScheduledJobStatus.PUBLISHING or task.status == BlogTaskStatus.POSTING:
        logger.info("예약 발행 결과 불명 | %s - 사람 확인 필요", short(job.job_id))
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.FAILED,
                    "error_code": "PUBLISH_RESULT_UNKNOWN",
                    "error_message": (
                        "발행 도중 서버가 재시작되어 결과를 확인하지 못했습니다. "
                        f"{where}를 확인한 뒤 필요하면 다시 시도해 주세요."
                    ),
                    "updated_at": now,
                }
            )
        )
        return True

    if task.status == BlogTaskStatus.POSTING_NEEDS_HUMAN:
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.NEEDS_HUMAN,
                    "error_code": "NAVER_NEEDS_HUMAN"
                    if job.publish_naver
                    else "THREADS_NEEDS_HUMAN",
                    "error_message": (
                        "네이버에서 추가 인증이 필요합니다."
                        if job.publish_naver
                        else "쓰레드에서 추가 인증이 필요합니다."
                    ),
                    "updated_at": now,
                }
            )
        )
        batch = await repository.find_batch(job.batch_id)
        if batch is not None:
            await repository.save_batch(
                batch.model_copy(
                    update={"status": ScheduledBatchStatus.NEEDS_HUMAN, "updated_at": now}
                )
            )
        return True

    # 절대 시각 예약이고 원고까지 만들어 뒀다면, 남은 일은 **그 시각에 올리는 것**뿐이다.
    # 여기서 WAITING으로 되돌리면 화면은 '대기'로 보이고 워커는 준비를 처음부터 다시
    # 집어 간다 — 결과는 같지만(생성 단계는 원고가 있어 건너뛴다) 재시작 때마다 상태가
    # 한 칸 뒤로 가는 것처럼 보인다. 예약 시각(publish_at)은 DB에 그대로 있으므로
    # 재시작 뒤에도 같은 시각에 올라간다.
    # 원고까지 만들어 둔 작업. **원고는 그대로 두되 발행을 자동으로 잇지 않는다**
    # (2026-08-12 사용자 지시: "서버가 다시 켜진다고 해서 이어서 진행하고 그런 거 없어").
    #
    # 예전에는 여기서 READY_TO_PUBLISH로 세워 두었고, 그러면 워커가 곧바로 집어 올렸다 —
    # 사용자가 모르는 사이에 글이 올라간다. 실패로 세우면 만든 원고는 남고(generated_at을
    # 그대로 들고 간다) 작업 큐에서 재시도를 눌러야 올라간다.
    if job.publish_at and task.final_post is not None:
        if job.status == ScheduledJobStatus.FAILED and job.error_code == "STOPPED_BY_RESTART":
            return False
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.FAILED,
                    "stage": _publish_stage(job),
                    "generated_at": job.generated_at or now,
                    "error_code": "STOPPED_BY_RESTART",
                    "error_message": (
                        "서버가 재시작되어 작업이 멈췄습니다. 원고는 그대로 있으니 "
                        "확인한 뒤 다시 시도해 주세요."
                    ),
                    "updated_at": now,
                }
            )
        )
        return True

    # **작업 중이던 것은 되살리지 않는다**(2026-08-12 사용자 지시: "예약작업도 멈추는
    # 게 맞아"). 서버가 꺼질 때 원고를 만들고 있었다면 그 실행은 사라졌고, 되살리면
    # 사용자가 모르는 사이에 다시 돈다. 실패로 세워 두면 작업 큐에서 보이고, 사람이
    # 재시도를 눌러야 다시 시작한다.
    #
    # **아직 시작하지 않은 작업은 그대로 둔다.** 시각이 오지 않아 기다리던 것까지 실패로
    # 만들면, 서버를 한 번 재시작했다는 이유로 걸어 둔 예약이 통째로 날아간다.
    if job.status == ScheduledJobStatus.RUNNING or task.status == BlogTaskStatus.GENERATING:
        logger.info("예약 원고 생성 중단 | %s - 재시작으로 정지", short(job.job_id))
        await repository.save_job(
            job.model_copy(
                update={
                    "status": ScheduledJobStatus.FAILED,
                    # 어디까지 갔는지는 BlogTask가 안다 — 원고가 이미 있으면 발행 단계로
                    # 세워 둔다(멈췄어도 만든 것은 남는다).
                    "stage": _stage_for(task, job),
                    "generated_at": job.generated_at
                    or (now if task.final_post is not None else None),
                    "error_code": "STOPPED_BY_RESTART",
                    "error_message": (
                        "서버가 재시작되어 작업이 멈췄습니다. 다시 시도해 주세요."
                    ),
                    "updated_at": now,
                }
            )
        )
        return True

    # 그 밖에는 전부 '다시 대기'다. 어디까지 갔는지는 실행 시점에 BlogTask를 보고
    # 판단한다(원고가 있으면 발행부터, 의도가 있으면 원고부터) — stage는 표시용이다.
    stage = _stage_for(task, job)
    # 원고가 이미 있으면 그 사실을 generatedAt에 남긴다 — 화면의 미리보기가 이 값으로
    # 열리므로, 앞선 실행에서 만든 원고도 재시작 뒤에 볼 수 있어야 한다.
    generated_at = job.generated_at or (now if task.final_post is not None else None)
    if (
        job.status == ScheduledJobStatus.WAITING
        and job.stage == stage
        and job.generated_at == generated_at
    ):
        return False
    await repository.save_job(
        job.model_copy(
            update={
                "status": ScheduledJobStatus.WAITING,
                "stage": stage,
                "generated_at": generated_at,
                "updated_at": now,
            }
        )
    )
    return True


def _publish_stage(job: ScheduledJob) -> ScheduledJobStage:
    """이 작업의 발행 단계 이름. 쓰레드 단독 예약은 네이버 칸을 지나지 않는다."""
    return (
        ScheduledJobStage.NAVER_PUBLISH
        if job.publish_naver
        else ScheduledJobStage.THREADS_PUBLISH
    )


def _stage_for(task, job: ScheduledJob | None = None) -> ScheduledJobStage:
    """BlogTask의 실제 상태로 표시용 단계를 정한다.

    ``job``은 원고까지 끝난 글의 발행 칸 이름을 가를 때만 쓴다(쓰레드 단독 예약은
    네이버 칸을 지나지 않는다). 주지 않으면 예전과 같이 네이버 기준이다.
    """
    if task.final_post is not None:
        return _publish_stage(job) if job is not None else ScheduledJobStage.NAVER_PUBLISH
    if task.selected_intent is not None:
        return ScheduledJobStage.DRAFT_GENERATION
    if task.intent_validation_result is not None:
        return ScheduledJobStage.INTENT_SELECTION
    if task.trend_selection is not None:
        return ScheduledJobStage.SEARCH_ANALYSIS
    if task.status == BlogTaskStatus.REFERENCE_PROCESSING:
        return ScheduledJobStage.TREND_RECOMMENDATION
    return ScheduledJobStage.CREATE_POST
