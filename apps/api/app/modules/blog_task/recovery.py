"""프로세스가 죽으면서 진행 중인 채로 남은 작업을 되살린다.

M3 검증과 M4 원고는 FastAPI 백그라운드 작업으로 돈다. 그 프로세스가 배포·크래시·재시작으로
사라지면 작업은 사라지는데 글의 상태는 `SEARCH_ANALYZING` / `GENERATING`에 그대로 남는다.
화면은 그 상태를 '지금 돌고 있음'으로 읽으므로, 아무도 돌리고 있지 않은 작업의 스피너가
영원히 돈다. 사용자가 할 수 있는 일이 없다 — 버튼은 이미 진행 중이라며 막혀 있다.

그래서 시작할 때 한 번 훑는다. **살아 있는 임차가 없는** 진행 중 작업만 회수 대상이다.
이 조건이 중요한 이유는 서버를 여러 대로 돌릴 때다: B 서버가 재시작한다고 해서 A 서버가
지금 돌리고 있는 작업을 실패로 만들면 안 된다.

임차만으로는 부족하다는 것이 2026-08-05에 드러났다 — 아래 ``FRESH_SECONDS`` 참고.
방금 시작한 작업은 이번 훑기에서 건너뛰고(``SweepResult.deferred``), 유예가 지난 뒤
한 번 더 훑는다(``services.recover_interrupted_jobs``).

되살리는 방식은 단계마다 다르다.

- `GENERATING`(M4): 직전 상태인 `INTENT_SELECTED`로 되돌린다. 사용자가 '원고 생성'을 다시
  누를 수 있는 자리다. 자동으로 재실행하지 않는 것은 의도적이다 — 재시작마다 원고와 이미지
  생성이 저절로 돌면 과금이 사용자 의사와 무관하게 발생한다.
- `SEARCH_ANALYZING`(M3): 상태는 그대로 두고 '검증 실패' 결과를 남긴다. 검증 팝업이 사유와
  함께 '다시 검증' 버튼을 보여주는 자리라, 사용자가 곧바로 다시 시도할 수 있다.

둘 다 진행 상황(progress)을 지운다. 남아 있으면 화면이 계속 단계 표시를 띄운다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.shared import BlogTaskStatus

from .locks import JobLease, lease_key

logger = logging.getLogger(__name__)

RECOVERY_ACTOR = "recovery"

# 지금 상태로 들어온 지 이만큼 안 된 작업은 이번 훑기에서 건드리지 않는다.
#
# 임차 하나에만 기대면, 임차가 잠깐 끊긴 사이에 **멀쩡히 돌고 있는** 작업이 회수된다.
# 2026-08-05에 실제로 그랬다: 5분짜리 원고 생성이 4분째일 때 두 번째 서버 인스턴스가
# 떴고(uvicorn은 포트를 잡기 전에 시작 훅을 먼저 돌린다 — 포트 충돌로 곧 죽어도 이
# 스위퍼는 이미 돈 뒤다) 그 글이 INTENT_SELECTED로 되돌려졌다. 원고는 정상 완성돼
# 저장까지 됐는데 화면만 '생성 실패'를 띄웠다.
#
# M4는 이미지까지 5분 남짓 걸린다. 그보다 넉넉히 잡는다.
FRESH_SECONDS = 900

# 되살릴 대상 상태와, 그 작업이 임차를 잡을 때 쓰는 단계 이름(locks.lease_key와 같아야 한다).
_PHASE_BY_STATUS = {
    BlogTaskStatus.SEARCH_ANALYZING: "m3",
    BlogTaskStatus.GENERATING: "m4",
}


@dataclass
class SweepResult:
    """되살린 글 수와, 방금 시작한 것 같아 유예한 글 수."""

    recovered: int = 0
    deferred: int = 0


def _entered_current_status_at(task) -> str | None:
    """지금 상태로 들어온 시각. 이력이 없는 옛 문서는 updatedAt으로 갈음한다."""
    for entry in reversed(task.status_history or []):
        if entry.to == task.status:
            return entry.at
    return getattr(task, "updated_at", None)


def _is_fresh(task, now: datetime) -> bool:
    stamp = _entered_current_status_at(task)
    if not stamp:
        return False
    try:
        entered = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        # 시각을 읽을 수 없으면 유예하지 않는다 — 판단 근거가 없을 때는 '스피너가 영영
        # 도는' 쪽보다 '되살려 사용자가 다시 누를 수 있는' 쪽이 낫다(예전과 같은 동작).
        return False
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=timezone.utc)
    return now - entered < timedelta(seconds=FRESH_SECONDS)


async def recover_orphaned_tasks(
    repository, lease: JobLease, failed_result_factory, now: datetime | None = None
) -> SweepResult:
    """되살린 글 수와 유예한 글 수. 실패해도 서버 시작을 막지 않는다.

    ``failed_result_factory(post_id, blog_input, error)``는 M3 실패 결과를 만드는 함수다
    (blog_task.service가 쓰는 것과 같은 것을 넘겨 화면 문구가 갈라지지 않게 한다).
    ``now``는 테스트용 — 비우면 현재 시각이다.
    """
    try:
        stuck = await repository.list_by_status(list(_PHASE_BY_STATUS))
    except Exception as error:
        logger.warning("작업 복구: 진행 중 목록 조회 실패(%s). 건너뜁니다.", error)
        return SweepResult()

    moment = now or datetime.now(timezone.utc)
    result = SweepResult()
    for task in stuck:
        phase = _PHASE_BY_STATUS.get(task.status)
        if phase is None:
            continue
        try:
            if await lease.is_held(lease_key(task.post_id, phase)):
                # 다른 프로세스가 지금 돌리고 있다 — 건드리지 않는다.
                continue
            if _is_fresh(task, moment):
                # 임차는 없다는데 시작한 지 얼마 안 됐다. 임차가 잠깐 끊긴 것일 수 있으므로
                # 지금은 두고, 유예가 지난 뒤 다시 본다.
                logger.info(
                    "작업 복구 유예 | %s - %s로 들어간 지 얼마 되지 않았습니다",
                    task.post_id,
                    task.status.value,
                )
                result.deferred += 1
                continue
            await _recover(repository, task, failed_result_factory)
            result.recovered += 1
        except Exception as error:
            # 한 글의 복구 실패가 나머지 복구와 서버 시작을 막지 않는다.
            logger.warning("작업 복구 실패 | %s - %s", task.post_id, error)

    if result.recovered:
        logger.info(
            "작업 복구: 진행 중인 채로 멈춰 있던 글 %d건을 되살렸습니다.", result.recovered
        )
    return result


async def _recover(repository, task, failed_result_factory) -> None:
    if task.status == BlogTaskStatus.GENERATING:
        await repository.transition_status(
            task.post_id, BlogTaskStatus.INTENT_SELECTED, RECOVERY_ACTOR
        )
    else:
        await repository.save_intent_validation_result(
            task.post_id,
            failed_result_factory(
                task.post_id,
                task.input,
                RuntimeError("서버가 다시 시작되어 검증이 중단되었습니다"),
            ),
        )
    await repository.update_progress(task.post_id, None)
