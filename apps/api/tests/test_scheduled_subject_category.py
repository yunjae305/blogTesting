"""예약에 **소재 분야**를 실어 보낸다(2026-08-12).

「예약 포스팅」 탭은 소재 줄마다 분야를 고를 수 있다 — '오디세이'가 영화인지 게임인지
모니터인지를 가르는 값이고, 새 글 작성에서 사용자가 직접 고르는 것과 같은 목록이다
(SUBJECT_CATEGORIES).

여기서 보는 것은 셋이다.

1. **고른 값이 끝까지 가는가.** 요청 → 작업 문서 → create_blog_task까지.
2. **안 고른 줄이 예전 그대로인가.** 값이 없으면 키 자체를 보내지 않는다 — 빈 문자열을
   보내면 create_blog_task가 목록 밖의 값이라고 거절한다.
3. **옛 것이 그대로 도는가.** 이 필드를 모르는 옛 요청·옛 작업 문서.
"""

import pytest

from app.errors import BlogTaskError
from app.modules.scheduled_posting.models import ScheduledJob
from app.modules.scheduled_posting.validation import validate_start_batch_request
from app.shared import SUBJECT_CATEGORIES

from test_scheduled_posting_service import (  # noqa: E402
    build_service,
    naver_saved,
)

# 이 파일은 동기 검증과 비동기 서비스 테스트가 섞여 있다. 모듈 전체에 asyncio 표시를
# 걸면 동기 테스트마다 경고가 뜨는데, pyproject의 asyncio_mode="auto"가 이미 async
# 함수를 알아서 돌려 준다.


def body(*schedules) -> dict:
    """「예약 포스팅」 탭이 보내는 몸통. 발행 시각이 없으므로 간격 방식이다."""
    return {
        "topics": [item["topic"] for item in schedules],
        "schedules": list(schedules),
        "intervalSeconds": 15,
        "platform": "naver",
        "topicMode": "multi",
    }


def row(topic: str, category: str | None = None) -> dict:
    item: dict = {"topic": topic, "publishNaver": True, "publishThreads": False}
    if category is not None:
        item["subjectCategory"] = category
    return item


# ------------------------------------------------------------------------ 검증


def test_목록에_있는_분야는_통과한다():
    request = validate_start_batch_request(body(row("오디세이", "게임")))

    assert request.schedules[0].subject_category == "게임"


def test_목록에_없는_분야는_거부한다():
    """자유 문자열을 받으면 프롬프트에 그대로 실려 모델이 뜻을 지어낸다."""
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(row("오디세이", "우주")))

    assert caught.value.code == "VALIDATION_FAILED"
    assert "subjectCategory" in caught.value.message


def test_빈_문자열은_고르지_않은_것으로_읽는다():
    """화면의 '분야 자동'이 빈 값이다. 그것을 목록 밖의 값이라고 막으면 안 된다."""
    request = validate_start_batch_request(body(row("오디세이", "")))

    assert request.schedules[0].subject_category is None


def test_문자열이_아니면_거부한다():
    with pytest.raises(BlogTaskError):
        validate_start_batch_request(body(row("오디세이", None) | {"subjectCategory": 3}))


def test_보내지_않은_옛_요청은_분야가_없다():
    """이 필드를 모르는 클라이언트가 보낸 요청. 예전 그대로 통과해야 한다."""
    request = validate_start_batch_request(body(row("오디세이")))

    assert request.schedules[0].subject_category is None


def test_화면_목록과_서버_목록이_같다():
    """화면(constants.ts)이 서버에 없는 값을 버튼으로 내보내면 그 버튼이 곧 저장 실패다."""
    assert "게임" in SUBJECT_CATEGORIES
    assert "IT·컴퓨터·AI" in SUBJECT_CATEGORIES


# ---------------------------------------------------------------- 작업에 저장


async def test_고른_분야가_작업에_저장된다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()

    view = await service.start_batch(
        "user_1", body(row("오디세이", "게임"), row("손흥민", "스포츠"))
    )

    assert [job.subject_category for job in view.jobs] == ["게임", "스포츠"]
    saved = await repository.list_jobs(view.batch.batch_id)
    assert [job.subject_category for job in saved] == ["게임", "스포츠"]


async def test_고르지_않은_줄은_비어_있다(monkeypatch):
    """한 배치 안에서 고른 줄과 안 고른 줄이 섞일 수 있다."""
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch("user_1", body(row("오디세이", "게임"), row("손흥민")))

    assert [job.subject_category for job in view.jobs] == ["게임", None]


async def test_소재만_보낸_옛_요청도_그대로_돈다(monkeypatch):
    """schedules 없이 topics만 보내던 옛 클라이언트. 작업은 만들어지고 분야만 없다."""
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1", {"topics": ["첫 소재", "둘째 소재"], "intervalSeconds": 600}
    )

    assert len(view.jobs) == 2
    assert all(job.subject_category is None for job in view.jobs)


def test_옛_작업_문서는_필드_없이_읽힌다():
    """돌고 있던 예약의 작업 문서에는 이 칸이 없다. 없다고 터지면 예약이 통째로 죽는다."""
    job = ScheduledJob(
        job_id="job_1",
        batch_id="batch_1",
        user_id="user_1",
        sequence=0,
        topic="오디세이",
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
    )

    assert job.subject_category is None


# ------------------------------------------------------------ 글을 만들 때


async def test_글을_만들_때_분야를_함께_넘긴다(monkeypatch):
    """작업 문서에만 있고 글에 넘어가지 않으면 아무것도 달라지지 않는다."""
    naver_saved(monkeypatch, True)
    service, _, world = build_service()
    view = await service.start_batch("user_1", body(row("오디세이", "게임")))

    await service.execute_job(view.jobs[0].job_id, publish=False)

    (created,) = world.args_of("create_blog_task")
    assert created["subjectCategory"] == "게임"
    assert created["topic"] == "오디세이"


async def test_고르지_않았으면_키를_아예_보내지_않는다(monkeypatch):
    """빈 값을 실어 보내면 create_blog_task가 목록 밖의 값이라고 거절한다."""
    naver_saved(monkeypatch, True)
    service, _, world = build_service()
    view = await service.start_batch("user_1", body(row("손흥민")))

    await service.execute_job(view.jobs[0].job_id, publish=False)

    (created,) = world.args_of("create_blog_task")
    assert "subjectCategory" not in created
