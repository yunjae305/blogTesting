"""자동 포스팅으로 거는 큐에도 브랜드를 얹는다(2026-08-19).

AIONA 유입용 콘텐츠는 한 편씩 손으로 만드는 것이 아니라 **6~10편을 큐로 걸어** 돌린다.
그 통로가 자동 포스팅인데, 여기에는 브랜드를 고를 자리가 없었다 — 새 글 작성으로 만든
글만 브랜드가 실리고, 큐로 나간 글은 브랜드 없이 나갔다.

여기서 지키는 것은 둘이다.

1. **큐로 나간 글도 화면으로 만든 글과 같은 처리를 받는다.** 브랜드 자료가 얹히고,
   역할이 활용(UTILITY)으로 잡히고, 결합 가능성이 재어진다. 두 통로가 갈라지면 그
   차이는 원고가 나오고 나서야 보인다.
2. **브랜드를 걸지 않은 예약은 한 글자도 달라지지 않는다.** 이 저장소에서 돌고 있는
   예약 대부분이 그쪽이다.
"""

import pytest

from app.modules.brand import BrandService, InMemoryBrandRepository
from app.modules.scheduled_posting.models import ScheduledJob
from app.modules.scheduled_posting.repository import InMemoryScheduledPostingRepository
from app.modules.scheduled_posting.service import ScheduledPostingService
from app.modules.scheduled_posting.validation import validate_start_batch_request
from app.shared import BrandProfile, BrandUseCase

from test_scheduled_posting_service import (  # noqa: E402
    FakeBlogTaskService,
    FakeDraftService,
    FakeTrendService,
    FakeWorld,
    naver_saved,
)


def body(*topics: str, brand_id: str | None = None) -> dict:
    """「자동 포스팅」 탭이 보내는 몸통. 발행 시각이 없으므로 간격 방식이다."""
    request: dict = {
        "topics": list(topics),
        "schedules": [
            {"topic": topic, "publishNaver": True, "publishThreads": False}
            for topic in topics
        ],
        "intervalSeconds": 15,
        "platform": "naver",
        "topicMode": "multi",
    }
    if brand_id is not None:
        request["brandId"] = brand_id
    return request


async def brand_service_with_aiona(user_id: str = "user_1") -> BrandService:
    repository = InMemoryBrandRepository()
    await repository.upsert(
        BrandProfile(
            brand_id="brand_1",
            user_id=user_id,
            name="AIONA",
            description="여러 AI 모델을 한자리에서 쓰는 서비스입니다.",
            created_at="2026-08-19T00:00:00.000Z",
            updated_at="2026-08-19T00:00:00.000Z",
            use_cases=[
                BrandUseCase(
                    situation="어떤 정보를 알아보고 싶을 때",
                    feature="자료 조사",
                    keywords=["다이어트", "칼로리", "성분"],
                )
            ],
        )
    )
    return BrandService(repository)


def build_service(brand_service=None):
    """브랜드를 아는 예약 서비스. 브랜드를 주지 않으면 예전 그대로다."""
    world = FakeWorld()
    repository = InMemoryScheduledPostingRepository()
    service = ScheduledPostingService(
        repository=repository,
        blog_task_service=FakeBlogTaskService(world),
        trend_service=FakeTrendService(world),
        draft_service=FakeDraftService(world),
        brand_service=brand_service,
    )
    return service, repository, world


# ------------------------------------------------------------------------ 검증


def test_브랜드를_보내면_요청에_담긴다():
    request = validate_start_batch_request(body("다이어트 간식", brand_id="brand_1"))

    assert request.brand_id == "brand_1"


def test_브랜드를_보내지_않은_옛_요청은_비어_있다():
    """돌고 있는 예약 대부분이 이쪽이다. 없는 값을 만들어 넣지 않는다."""
    request = validate_start_batch_request(body("다이어트 간식"))

    assert request.brand_id is None


def test_빈_문자열은_고르지_않은_것으로_읽는다():
    """화면의 '브랜드 없이'가 빈 값이다."""
    request = validate_start_batch_request(body("다이어트 간식", brand_id="  "))

    assert request.brand_id is None


def test_문자열이_아니면_거부한다():
    from app.errors import BlogTaskError

    with pytest.raises(BlogTaskError):
        validate_start_batch_request(body("다이어트 간식") | {"brandId": 3})


# ------------------------------------------------------------------- 작업까지


@pytest.mark.asyncio
async def test_배치의_브랜드가_작업마다_실린다(monkeypatch):
    """값은 작업이 들고 있는다 — 글을 만드는 것이 작업이기 때문이다.

    배치에서 찾아 쓰면 재시도·재예약 때마다 배치를 다시 읽어야 하고, 배치가 지워진 뒤의
    작업은 브랜드를 잃는다.
    """
    naver_saved(monkeypatch, True)
    service, _, _ = build_service(await brand_service_with_aiona())

    view = await service.start_batch("user_1", body("다이어트 간식", "빼빼로", brand_id="brand_1"))

    assert [job.brand_id for job in view.jobs] == ["brand_1", "brand_1"]
    # 화면이 배치를 다시 그릴 때 무엇으로 걸었는지 보여 줘야 한다.
    assert view.batch.brand_id == "brand_1"


@pytest.mark.asyncio
async def test_브랜드를_걸지_않으면_작업에도_없다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, _, _ = build_service(await brand_service_with_aiona())

    view = await service.start_batch("user_1", body("다이어트 간식"))

    assert [job.brand_id for job in view.jobs] == [None]
    assert view.batch.brand_id is None


@pytest.mark.asyncio
async def test_큐로_나간_글도_브랜드_자료를_받는다(monkeypatch):
    """화면으로 만든 글과 **같은 함수**를 거친다(with_brand_materials)."""
    naver_saved(monkeypatch, True)
    service, _, world = build_service(await brand_service_with_aiona())
    view = await service.start_batch("user_1", body("다이어트 간식", brand_id="brand_1"))

    await service.execute_job(view.jobs[0].job_id, publish=False)

    (created,) = world.args_of("create_blog_task")
    assert created["brandId"] == "brand_1"
    assert created["brandName"] == "AIONA"
    # 소재가 반드시 있는 흐름이라 브랜드는 언제나 **활용 도구**다.
    assert created["brandMode"] == "UTILITY"
    # 소재는 사용자가 큐에 적은 그대로다 — 브랜드 이름으로 덮이지 않는다.
    assert created["topic"] == "다이어트 간식"
    # 결합 가능성과 닿은 기준표 줄도 함께 간다.
    assert created["brandFitGrade"] == "A"
    assert created["brandUseCases"] == ["- 어떤 정보를 알아보고 싶을 때 → 자료 조사"]
    # 브랜드 자료가 참고자료로 펼쳐져 들어간다.
    assert any(m.get("origin") == "brand" for m in created["referenceMaterials"])


@pytest.mark.asyncio
async def test_브랜드_없는_예약은_예전_그대로다(monkeypatch):
    """돌고 있는 예약이 여기서 달라지면 안 된다 — 키 자체를 보내지 않는다."""
    naver_saved(monkeypatch, True)
    service, _, world = build_service(await brand_service_with_aiona())
    view = await service.start_batch("user_1", body("다이어트 간식"))

    await service.execute_job(view.jobs[0].job_id, publish=False)

    (created,) = world.args_of("create_blog_task")
    assert "brandId" not in created
    assert "brandMode" not in created
    assert "referenceMaterials" not in created


@pytest.mark.asyncio
async def test_브랜드_서비스가_없으면_조용히_브랜드_없이_만든다(monkeypatch):
    """이 서비스를 브랜드 없이 세우는 자리(부분 구성)를 깨지 않는다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service(brand_service=None)
    view = await service.start_batch("user_1", body("다이어트 간식", brand_id="brand_1"))
    # 작업에는 남지만, 만들 때 얹을 자료를 가져올 곳이 없다.
    assert view.jobs[0].brand_id == "brand_1"

    await service.execute_job(view.jobs[0].job_id, publish=False)

    (created,) = world.args_of("create_blog_task")
    assert "brandName" not in created


@pytest.mark.asyncio
async def test_옛_작업_문서에는_브랜드가_없다():
    """2026-08-19 이전에 걸린 작업. 없는 필드로 읽어도 죽지 않아야 한다."""
    job = ScheduledJob(
        job_id="job_1",
        batch_id="batch_1",
        user_id="user_1",
        sequence=0,
        topic="다이어트 간식",
        created_at="2026-08-18T00:00:00.000Z",
        updated_at="2026-08-18T00:00:00.000Z",
    )

    assert job.brand_id is None
