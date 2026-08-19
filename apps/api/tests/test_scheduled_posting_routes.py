"""예약 포스팅 라우트의 HTTP 계약.

여기서 막는 것은 세 가지다.

1. **소유권이 쿼리에 들어 있는지.** 남의 batchId·jobId를 알아도 읽히거나 재시도되면
   안 된다. 서비스가 ``find_user_batch``/``find_user_job``을 쓰는지는 저장소 테스트가
   보지만, 라우트가 인증된 사용자의 id를 넘기는지는 이 층에서만 드러난다.
2. **워커를 깨우는지.** 시작·일시정지·재개·정지·재시도는 전부 화면의 버튼이다. 깨우지
   않으면 최대 30초(MAX_SLEEP_SECONDS) 동안 아무 일도 일어나지 않아, 사용자에게는
   버튼이 먹지 않은 것으로 보인다.
3. **상태 코드.** 특히 NAVER_NOT_CONNECTED는 화면이 '설정으로 가기'로 분기하는 값이라
   409여야 한다. 500(원인 불명)이 되면 화면은 안내를 못 한다.

LLM·Mongo·셀레니움은 하나도 부르지 않는다. 저장소는 메모리 구현이고, 글 생성 쪽
서비스들은 부르면 그 자리에서 터지는 가짜다 — 라우트가 파이프라인을 건드리면 즉시
드러난다. 네이버 저장 여부만 monkeypatch로 갈아 끼운다(파일을 읽지 않기 위해서다).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import AuthService
from app.modules.scheduled_posting.models import (
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStatus,
)
from app.modules.scheduled_posting.repository import InMemoryScheduledPostingRepository
from app.modules.scheduled_posting.service import ScheduledPostingService
from app.posting import credentials as 자격증명모듈

비밀번호 = "password123"


def _지금() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------- 가짜들


class _부르면안되는서비스:
    """글 생성 파이프라인 자리. 라우트 테스트에서 여기까지 내려가면 안 된다."""

    def __init__(self, 이름: str):
        self._이름 = 이름

    def __getattr__(self, name: str):
        raise AssertionError(
            f"{self._이름}.{name} 이(가) HTTP 테스트에서 호출되었다 — "
            "라우트는 원고 파이프라인을 직접 부르지 않는다"
        )


class _기록워커:
    """워커 대역. ``wake()``가 몇 번 불렸는지만 센다."""

    def __init__(self) -> None:
        self.깨운횟수 = 0

    def wake(self) -> None:
        self.깨운횟수 += 1


class _기록서비스:
    """진짜 서비스를 감싸, 라우트가 **어느 메서드를 어떤 인자로** 불렀는지 남긴다.

    동작은 진짜 서비스 그대로다 — 응답 본문까지 함께 검사할 수 있어야 하므로 흉내만
    내는 가짜로 바꾸지 않았다.
    """

    def __init__(self, 안쪽: ScheduledPostingService):
        self._안쪽 = 안쪽
        self.호출 = []

    def __getattr__(self, name: str):
        원본 = getattr(self._안쪽, name)

        async def 감싼(*args, **kwargs):
            self.호출.append((name, args, kwargs))
            return await 원본(*args, **kwargs)

        return 감싼


@dataclass
class _환경:
    app: Any
    저장소: InMemoryScheduledPostingRepository
    워커: _기록워커
    서비스: _기록서비스


def _환경만들기() -> _환경:
    저장소 = InMemoryScheduledPostingRepository()
    서비스 = _기록서비스(
        ScheduledPostingService(
            repository=저장소,
            blog_task_service=_부르면안되는서비스("blog_task_service"),
            trend_service=_부르면안되는서비스("trend_service"),
            draft_service=_부르면안되는서비스("draft_service"),
        )
    )
    워커 = _기록워커()
    app = create_app()
    app.state.services = SimpleNamespace(
        auth_service=AuthService(repository=InMemoryUserRepository()),
        scheduled_posting_service=서비스,
        scheduled_posting_worker=워커,
    )
    return _환경(app=app, 저장소=저장소, 워커=워커, 서비스=서비스)


def _클라이언트(환경: _환경) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=환경.app), base_url="http://test")


async def _가입하고_로그인(client: AsyncClient, 이메일: str, 별명: str) -> tuple[dict, str]:
    """(인증 헤더, userId)를 돌려준다."""
    가입 = await client.post(
        "/auth/signup", json={"email": 이메일, "password": 비밀번호, "nickname": 별명}
    )
    assert 가입.status_code == 201
    로그인 = await client.post("/auth/login", json={"email": 이메일, "password": 비밀번호})
    세션 = 로그인.json()
    return {"authorization": f"Bearer {세션['accessToken']}"}, 세션["user"]["userId"]


def _배치(
    batch_id: str,
    user_id: str,
    status: ScheduledBatchStatus = ScheduledBatchStatus.RUNNING,
    **덧붙임,
) -> ScheduledBatch:
    지금 = _지금()
    return ScheduledBatch(
        batch_id=batch_id,
        user_id=user_id,
        status=status,
        target_count=1,
        interval_seconds=1800,
        total_count=1,
        created_at=지금,
        updated_at=지금,
        **덧붙임,
    )


def _작업(
    job_id: str,
    batch_id: str,
    user_id: str,
    status: ScheduledJobStatus = ScheduledJobStatus.FAILED,
    sequence: int = 0,
    topic: str = "겨울 캠핑 난방 준비물",
) -> ScheduledJob:
    지금 = _지금()
    return ScheduledJob(
        job_id=job_id,
        batch_id=batch_id,
        user_id=user_id,
        sequence=sequence,
        topic=topic,
        status=status,
        created_at=지금,
        updated_at=지금,
    )


def _네이버_저장됨(monkeypatch, 저장됨: bool) -> None:
    """``_naver_saved``가 보는 값만 갈아 끼운다. 실제 파일은 읽지 않는다."""
    monkeypatch.setattr(
        자격증명모듈, "saved_username", lambda profile_dir: "네이버아이디" if 저장됨 else None
    )


# ----------------------------------------------------------------- 1. 인증


@pytest.mark.parametrize(
    ("메서드", "경로"),
    [
        ("POST", "/scheduled/naver/batches"),
        ("GET", "/scheduled/naver/batches/active"),
        ("GET", "/scheduled/naver/batches/batch_1"),
        ("POST", "/scheduled/naver/batches/batch_1/pause"),
        ("POST", "/scheduled/naver/batches/batch_1/resume"),
        ("POST", "/scheduled/naver/batches/batch_1/stop"),
        ("POST", "/scheduled/naver/jobs/job_1/retry"),
    ],
)
async def test_인증_없이_예약_라우트를_부르면_401이다(메서드, 경로):
    """토큰이 없으면 서비스까지 내려가지도 않아야 한다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        응답 = await client.request(메서드, 경로)

    assert 응답.status_code == 401
    assert 응답.json()["errorCode"] == "UNAUTHORIZED"
    # 인증 전에 예약 서비스나 워커를 건드리면 안 된다.
    assert 환경.서비스.호출 == []
    assert 환경.워커.깨운횟수 == 0


# --------------------------------------------------------------- 2·3. 소유권


async def test_남의_배치는_아이디를_알아도_404다():
    """소유권이 쿼리의 일부다 — 문서를 읽고 나중에 비교하는 방식이 아니다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(_배치("batch_주인", 주인), [])

        응답 = await client.get("/scheduled/naver/batches/batch_주인", headers=남의헤더)

    assert 응답.status_code == 404
    assert 응답.json()["errorCode"] == "NOT_FOUND"
    # 남의 배치의 존재 여부·내용이 응답에 새지 않는다.
    assert "batch" not in 응답.json()


async def test_자기_배치는_배치와_작업을_함께_돌려준다():
    """404가 '라우트가 죽어서'가 아니라 '소유권 때문'임을 같은 자리에서 보인다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        await 환경.저장소.create_batch(
            _배치("batch_주인", 주인), [_작업("job_1", "batch_주인", 주인)]
        )

        응답 = await client.get("/scheduled/naver/batches/batch_주인", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    assert 본문["batch"]["batchId"] == "batch_주인"
    assert 본문["batch"]["userId"] == 주인
    assert [작업["jobId"] for 작업 in 본문["jobs"]] == ["job_1"]


async def test_남의_작업은_재시도해도_404다():
    """jobId를 알아내도 남의 작업을 다시 돌릴 수 없다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(
            _배치("batch_주인", 주인, status=ScheduledBatchStatus.FAILED),
            [_작업("job_주인", "batch_주인", 주인)],
        )

        응답 = await client.post("/scheduled/naver/jobs/job_주인/retry", headers=남의헤더)

    assert 응답.status_code == 404
    assert 응답.json()["errorCode"] == "NOT_FOUND"
    # 남의 작업은 상태가 그대로여야 한다 — 재시도가 절반이라도 반영되면 안 된다.
    남겨진 = await 환경.저장소.find_job("job_주인")
    assert 남겨진.status == ScheduledJobStatus.FAILED
    assert 남겨진.retry_count == 0


async def test_자기_작업의_재시도는_대기로_되돌리고_워커를_깨운다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        await 환경.저장소.create_batch(
            _배치("batch_주인", 주인, status=ScheduledBatchStatus.FAILED),
            [_작업("job_주인", "batch_주인", 주인)],
        )

        응답 = await client.post("/scheduled/naver/jobs/job_주인/retry", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    assert 본문["jobs"][0]["status"] == "WAITING"
    assert 본문["jobs"][0]["retryCount"] == 1
    # 실패로 닫혔던 배치가 다시 열린다.
    assert 본문["batch"]["status"] == "RUNNING"
    assert 환경.서비스.호출 == [("retry_job", (주인, "job_주인"), {})]
    assert 환경.워커.깨운횟수 == 1


# ------------------------------------------------------------------ 4. 시작


async def test_예약_시작은_201과_배치_뷰를_돌려주고_워커를_깨운다(monkeypatch):
    """워커를 깨우지 않으면 첫 작업이 최대 30초 뒤에야 시작한다."""
    환경 = _환경만들기()
    _네이버_저장됨(monkeypatch, True)

    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        응답 = await client.post(
            "/scheduled/naver/batches",
            headers=헤더,
            json={
                "topics": ["겨울 캠핑 난방", "  ", "제습기 고르는 법"],
                "targetCount": 2,
                "intervalSeconds": 15,
            },
        )

    assert 응답.status_code == 201
    본문 = 응답.json()
    assert 본문["batch"]["userId"] == 사용자
    assert 본문["batch"]["status"] == "READY"
    assert 본문["batch"]["totalCount"] == 2
    assert 본문["batch"]["intervalSeconds"] == 15
    # 빈 줄은 소재로 세지 않는다. 순서는 사용자가 입력한 그대로다.
    assert [작업["topic"] for 작업 in 본문["jobs"]] == ["겨울 캠핑 난방", "제습기 고르는 법"]
    assert [작업["sequence"] for 작업 in 본문["jobs"]] == [0, 1]
    assert all(작업["status"] == "WAITING" for 작업 in 본문["jobs"])
    assert 환경.서비스.호출[0][0] == "start_batch"
    assert 환경.워커.깨운횟수 == 1


# ------------------------------------------------------------- 5. 활성 배치


async def test_활성_배치가_없으면_null을_돌려준다():
    """화면은 이 null로 '예약 시작' 버튼을 되살린다 — 404나 빈 객체가 아니다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, _ = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        응답 = await client.get("/scheduled/naver/batches/active", headers=헤더)

    assert 응답.status_code == 200
    assert 응답.json() is None


async def test_남의_활성_배치는_내_활성_배치가_아니다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(_배치("batch_주인", 주인), [])

        내것 = await client.get("/scheduled/naver/batches/active", headers=남의헤더)

    assert 내것.status_code == 200
    assert 내것.json() is None


async def test_활성_배치가_있으면_그_배치를_돌려준다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.RUNNING),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.get("/scheduled/naver/batches/active", headers=헤더)

    assert 응답.status_code == 200
    assert 응답.json()["batch"]["batchId"] == "batch_1"
    assert 응답.json()["jobs"][0]["jobId"] == "job_1"


# --------------------------------------------------------- 6. 일시정지·재개·정지


async def test_일시정지는_request_pause를_부르고_워커를_깨운다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.RUNNING), []
        )

        응답 = await client.post("/scheduled/naver/batches/batch_1/pause", headers=헤더)

    assert 응답.status_code == 200
    # 실행 중인 호출을 끊지 않는다 — 요청 표시만 남기고 워커가 다음 지점에서 멈춘다.
    assert 응답.json()["batch"]["status"] == "PAUSE_REQUESTED"
    assert 응답.json()["batch"]["pauseRequested"] is True
    assert 환경.서비스.호출 == [("request_pause", (사용자, "batch_1"), {})]
    assert 환경.워커.깨운횟수 == 1


async def test_재개는_resume을_부르고_워커를_깨운다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.PAUSED), []
        )

        응답 = await client.post("/scheduled/naver/batches/batch_1/resume", headers=헤더)

    assert 응답.status_code == 200
    assert 응답.json()["batch"]["status"] == "RUNNING"
    # 같은 batchId를 그대로 쓴다 — 재개가 새 배치를 만들면 진행률이 처음으로 돌아간다.
    assert 응답.json()["batch"]["batchId"] == "batch_1"
    assert 환경.서비스.호출 == [("resume", (사용자, "batch_1"), {})]
    assert 환경.워커.깨운횟수 == 1


async def test_정지는_request_stop을_부르고_워커를_깨운다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.RUNNING),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.post("/scheduled/naver/batches/batch_1/stop", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    # 돌고 있는 작업이 없으므로 그 자리에서 닫힌다.
    assert 본문["batch"]["status"] == "STOPPED"
    assert 본문["jobs"][0]["status"] == "CANCELED"
    assert 환경.서비스.호출 == [("request_stop", (사용자, "batch_1"), {})]
    assert 환경.워커.깨운횟수 == 1


@pytest.mark.parametrize("동작", ["pause", "resume", "stop"])
async def test_남의_배치는_일시정지도_재개도_정지도_못_한다(동작):
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(
            _배치("batch_주인", 주인, status=ScheduledBatchStatus.RUNNING), []
        )

        응답 = await client.post(
            f"/scheduled/naver/batches/batch_주인/{동작}", headers=남의헤더
        )

    assert 응답.status_code == 404
    assert 응답.json()["errorCode"] == "NOT_FOUND"
    남겨진 = await 환경.저장소.find_batch("batch_주인")
    assert 남겨진.status == ScheduledBatchStatus.RUNNING


# ------------------------------------------------- 7. NAVER_NOT_CONNECTED → 409


async def test_네이버_미연결은_500이_아니라_409로_나간다(monkeypatch):
    """화면이 이 코드로 '설정으로 가기'를 안내한다. 500이면 안내할 수 없다."""
    환경 = _환경만들기()
    _네이버_저장됨(monkeypatch, False)

    async with _클라이언트(환경) as client:
        헤더, _ = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        응답 = await client.post(
            "/scheduled/naver/batches",
            headers=헤더,
            json={"topics": ["겨울 캠핑 난방"], "targetCount": 1, "intervalSeconds": 1800},
        )

    assert 응답.status_code == 409
    assert 응답.json()["errorCode"] == "NAVER_NOT_CONNECTED"
    assert 응답.json()["success"] is False
    # 실패했으면 워커를 깨우지 않는다 — 깨울 배치가 없다.
    assert 환경.워커.깨운횟수 == 0


async def test_잘못된_요청은_400이고_배치가_만들어지지_않는다(monkeypatch):
    """간격을 빼먹은 요청. 400과 409를 구분해야 화면이 다른 안내를 할 수 있다."""
    환경 = _환경만들기()
    _네이버_저장됨(monkeypatch, True)

    async with _클라이언트(환경) as client:
        헤더, _ = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        응답 = await client.post(
            "/scheduled/naver/batches",
            headers=헤더,
            json={"topics": ["겨울 캠핑 난방"], "targetCount": 1},
        )

        확인 = await client.get("/scheduled/naver/batches/active", headers=헤더)

    assert 응답.status_code == 400
    assert 응답.json()["errorCode"] == "VALIDATION_FAILED"
    assert 확인.json() is None
    assert 환경.워커.깨운횟수 == 0


# ---------------------------------------------------------------- 8. userId


async def test_요청_본문의_userId는_무시되고_인증된_사용자의_것이_쓰인다(monkeypatch):
    """본문의 userId를 믿으면 남의 이름으로 예약을 만들 수 있다."""
    환경 = _환경만들기()
    _네이버_저장됨(monkeypatch, True)

    async with _클라이언트(환경) as client:
        _, 남 = await _가입하고_로그인(client, "other@blog-it.test", "남")
        헤더, 나 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")

        응답 = await client.post(
            "/scheduled/naver/batches",
            headers=헤더,
            json={
                "userId": 남,
                "topics": ["겨울 캠핑 난방"],
                "targetCount": 1,
                "intervalSeconds": 1800,
            },
        )

    assert 응답.status_code == 201
    본문 = 응답.json()
    assert 본문["batch"]["userId"] == 나
    assert [작업["userId"] for 작업 in 본문["jobs"]] == [나]
    # 서비스에도 인증된 사용자의 id가 넘어갔다.
    assert 환경.서비스.호출[0][1][0] == 나
    # 저장된 것도 마찬가지 — 남의 활성 배치로 잡히면 그 사람이 예약을 못 시작한다.
    assert await 환경.저장소.find_active_batch(남) is None
    assert (await 환경.저장소.find_active_batch(나)).batch_id == 본문["batch"]["batchId"]


# ------------------------------------------------------------- 9. 작업 삭제
#
# 사용자가 원한 것: "소재 1·2·3을 넣었는데 2는 쓰고 싶지 않다. 2를 지우면 1·3만 이어서
# 작성되게." 그래서 여기서 보는 것은 네 가지다.
#
# - 남의 jobId로는 못 지운다(그리고 지워진 척도 안 한다 — 그 작업이 **그대로 남아야** 한다).
# - 지운 뒤 응답의 jobs에서 곧바로 빠진다. 화면은 이 응답으로 표를 다시 그린다.
# - 워커를 깨운다. 안 깨우면 최대 30초 동안 큐가 멈춘 것처럼 보인다.
# - 지금 글을 쓰거나 발행하는 중인 작업은 400대로 거절한다. 500이면 화면이 "왜 안 되는지"를
#   말해 줄 수 없고, 사용자는 될 때까지 버튼을 다시 누른다.


def _소재세개_배치(
    batch_id: str,
    user_id: str,
    status: ScheduledBatchStatus = ScheduledBatchStatus.RUNNING,
) -> ScheduledBatch:
    """소재 3개짜리 배치. 기존 ``_배치``는 1개 기준이라 개수만 바꿔 쓴다.

    ``_배치``에 개수를 인자로 넘길 수는 없다(그 헬퍼가 이미 고정값으로 넘긴다). 헬퍼를
    고치면 이 파일의 다른 테스트가 함께 흔들리므로 만들어진 모델만 복사해 바꾼다.
    """
    return _배치(batch_id, user_id, status=status).model_copy(
        update={"target_count": 3, "total_count": 3}
    )


def _소재세개_작업(
    batch_id: str,
    user_id: str,
    상태들: tuple[ScheduledJobStatus, ...] = (
        ScheduledJobStatus.WAITING,
        ScheduledJobStatus.WAITING,
        ScheduledJobStatus.WAITING,
    ),
) -> list[ScheduledJob]:
    return [
        _작업(
            f"job_{번호}",
            batch_id,
            user_id,
            status=상태,
            sequence=번호 - 1,
            topic=f"소재 {번호}",
        )
        for 번호, 상태 in enumerate(상태들, start=1)
    ]


async def test_인증_없이_작업을_삭제하면_401이다():
    """삭제는 되돌릴 수 없다 — 토큰 없이 서비스까지 내려가면 안 된다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        응답 = await client.delete("/scheduled/naver/jobs/job_1")

    assert 응답.status_code == 401
    assert 응답.json()["errorCode"] == "UNAUTHORIZED"
    assert 환경.서비스.호출 == []
    assert 환경.워커.깨운횟수 == 0


async def test_남의_작업은_삭제해도_404이고_그대로_남는다():
    """jobId를 알아내도 남의 큐에서 소재를 뺄 수 없다.

    404만 보고 끝내지 않는다 — '거절했다'와 '지우고 나서 404를 줬다'는 응답이 같기 때문에,
    저장소에 그 작업이 **그대로 있는지**까지 본다.
    """
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_주인", 주인),
            _소재세개_작업("batch_주인", 주인),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_2", headers=남의헤더)

    assert 응답.status_code == 404
    assert 응답.json()["errorCode"] == "NOT_FOUND"

    남겨진 = await 환경.저장소.find_job("job_2")
    assert 남겨진 is not None
    assert 남겨진.status == ScheduledJobStatus.WAITING
    assert 남겨진.topic == "소재 2"
    # 배치의 개수도 손대지 않는다 — 절반만 반영되면 진행률이 어긋난다.
    남은배치 = await 환경.저장소.find_batch("batch_주인")
    assert 남은배치.total_count == 3
    assert [작업.job_id for 작업 in await 환경.저장소.list_jobs("batch_주인")] == [
        "job_1",
        "job_2",
        "job_3",
    ]
    assert 환경.워커.깨운횟수 == 0


async def test_가운데_소재를_삭제하면_응답의_jobs에서_빠지고_앞뒤가_남는다():
    """소재 1·2·3 중 2만 뺀다. 남은 1·3의 순서는 그대로다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_1", 사용자),
            _소재세개_작업("batch_1", 사용자),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_2", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    assert [작업["jobId"] for 작업 in 본문["jobs"]] == ["job_1", "job_3"]
    assert [작업["topic"] for 작업 in 본문["jobs"]] == ["소재 1", "소재 3"]
    # 번호를 다시 매기지 않는다. 워커는 sequence 순으로 다음 WAITING을 집으므로
    # 0·2로 남아도 1 다음이 3이다.
    assert [작업["sequence"] for 작업 in 본문["jobs"]] == [0, 2]
    # 개수는 남은 작업에서 다시 센다. 글의 개수도 남은 소재 수를 넘을 수 없다.
    assert 본문["batch"]["totalCount"] == 2
    assert 본문["batch"]["targetCount"] == 2
    # 배치는 계속 돈다 — 하나를 뺐다고 예약이 끝나면 안 된다.
    assert 본문["batch"]["status"] == "RUNNING"

    # 응답만 그런 것이 아니라 저장소에서도 사라졌다.
    assert await 환경.저장소.find_job("job_2") is None
    assert [작업.job_id for 작업 in await 환경.저장소.list_jobs("batch_1")] == [
        "job_1",
        "job_3",
    ]


async def test_삭제한_소재는_다시_조회해도_없다():
    """삭제가 응답에서만 반영되고 저장이 안 되면, 2초 뒤 폴링에서 되살아난 것처럼 보인다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_1", 사용자),
            _소재세개_작업("batch_1", 사용자),
        )

        await client.delete("/scheduled/naver/jobs/job_2", headers=헤더)
        다시 = await client.get("/scheduled/naver/batches/active", headers=헤더)

    assert 다시.status_code == 200
    assert [작업["topic"] for 작업 in 다시.json()["jobs"]] == ["소재 1", "소재 3"]


async def test_작업_삭제는_delete_job을_부르고_워커를_깨운다():
    """깨우지 않으면 최대 30초(MAX_SLEEP_SECONDS) 동안 큐가 멈춘 것처럼 보인다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_1", 사용자),
            _소재세개_작업("batch_1", 사용자),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_2", headers=헤더)

    assert 응답.status_code == 200
    # 인증된 사용자의 id가 그대로 넘어간다 — 경로의 jobId만 믿으면 소유권 검사가 없다.
    assert 환경.서비스.호출 == [("delete_job", (사용자, "job_2"), {})]
    assert 환경.워커.깨운횟수 == 1


@pytest.mark.parametrize(
    "상태", [ScheduledJobStatus.RUNNING, ScheduledJobStatus.PUBLISHING]
)
async def test_글을_쓰거나_발행하는_중인_작업_삭제는_500이_아니라_400이다(상태):
    """도는 중인 LLM·셀레니움을 버리면 네이버에 올라갔는지 알 수 없는 글이 남는다.

    거절 자체보다 **거절의 모양**이 중요하다. 500이면 화면이 이유를 말할 수 없어
    사용자는 될 때까지 휴지통을 다시 누른다.
    """
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_1", 사용자).model_copy(
                update={"current_job_id": "job_2"}
            ),
            _소재세개_작업(
                "batch_1",
                사용자,
                상태들=(ScheduledJobStatus.COMPLETED, 상태, ScheduledJobStatus.WAITING),
            ),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_2", headers=헤더)

    assert 응답.status_code != 500
    assert 400 <= 응답.status_code < 500
    assert 응답.status_code == 400
    assert 응답.json()["errorCode"] == "VALIDATION_FAILED"
    assert 응답.json()["success"] is False

    # 거절했으면 아무것도 바뀌지 않아야 한다.
    남겨진 = await 환경.저장소.find_job("job_2")
    assert 남겨진 is not None
    assert 남겨진.status == 상태
    assert (await 환경.저장소.find_batch("batch_1")).total_count == 3
    # 실패한 요청으로 워커를 깨우지 않는다.
    assert 환경.워커.깨운횟수 == 0


async def test_이미_발행된_작업도_내역에서_지울_수_있다():
    """발행 내역을 직접 정리할 수 있어야 한다(2026-08-06 사용자 요청).

    지워지는 것은 예약 기록 한 줄뿐이다 — 네이버 게시물도, '내 글 목록'의 원고도
    그대로 남는다(service._delete_backing_post가 올라간 글은 지우지 않는다).
    """
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _소재세개_배치("batch_1", 사용자),
            _소재세개_작업(
                "batch_1",
                사용자,
                상태들=(
                    ScheduledJobStatus.COMPLETED,
                    ScheduledJobStatus.WAITING,
                    ScheduledJobStatus.WAITING,
                ),
            ),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_1", headers=헤더)

    assert 응답.status_code == 200
    assert 환경.서비스.호출 == [("delete_job", (사용자, "job_1"), {})]


async def test_실패한_작업을_빼면_실패_수도_함께_줄어든다():
    """개수를 하나씩 빼고 더하면 언젠가 어긋난다 — 남은 작업에서 다시 세는지 본다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        배치 = _소재세개_배치("batch_1", 사용자).model_copy(update={"failed_count": 1})
        await 환경.저장소.create_batch(
            배치,
            _소재세개_작업(
                "batch_1",
                사용자,
                상태들=(
                    ScheduledJobStatus.FAILED,
                    ScheduledJobStatus.WAITING,
                    ScheduledJobStatus.WAITING,
                ),
            ),
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_1", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    assert 본문["batch"]["failedCount"] == 0
    assert 본문["batch"]["totalCount"] == 2
    assert [작업["topic"] for 작업 in 본문["jobs"]] == ["소재 2", "소재 3"]


async def test_마지막_남은_작업을_빼면_배치가_닫혀_다음_예약을_막지_않는다():
    """빈 배치를 열어 두면 활성 배치로 남아 '예약 시작'이 계속 막힌다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.RUNNING),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.delete("/scheduled/naver/jobs/job_1", headers=헤더)
        활성 = await client.get("/scheduled/naver/batches/active", headers=헤더)

    assert 응답.status_code == 200
    본문 = 응답.json()
    assert 본문["jobs"] == []
    assert 본문["batch"]["status"] == "STOPPED"
    # 글의 개수는 0으로 내려가지 않는다 — 저장 검증기가 1 미만을 받지 않는다.
    assert 본문["batch"]["targetCount"] >= 1
    assert 활성.json() is None
    assert await 환경.저장소.find_active_batch(사용자) is None


async def test_없는_작업을_삭제하면_404이고_워커를_깨우지_않는다():
    """이미 지운 것을 두 번 누른 경우다. 500이 아니라 404여야 한다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자, status=ScheduledBatchStatus.RUNNING), []
        )

        응답 = await client.delete("/scheduled/naver/jobs/없는작업", headers=헤더)

    assert 응답.status_code == 404
    assert 응답.json()["errorCode"] == "NOT_FOUND"
    assert 환경.워커.깨운횟수 == 0


# ------------------------------------------- 6. 예약 시각(목록·변경·취소)


def _앞으로(초: int) -> str:
    from datetime import timedelta

    시각 = datetime.now(timezone.utc) + timedelta(seconds=초)
    return 시각.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.mark.parametrize(
    ("메서드", "경로"),
    [
        ("GET", "/scheduled/naver/jobs"),
        ("PATCH", "/scheduled/naver/jobs/job_1"),
        ("POST", "/scheduled/naver/jobs/job_1/cancel"),
    ],
)
async def test_예약_시각_라우트도_인증을_요구한다(메서드, 경로):
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        응답 = await client.request(메서드, 경로)

    assert 응답.status_code == 401
    assert 환경.서비스.호출 == []


async def test_예약_목록은_내_것만_내려준다():
    """소유권이 쿼리의 일부다 — 남의 예약이 목록에 섞이면 안 된다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        _, 남 = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자),
            [
                _작업("job_내것", "batch_1", 사용자, status=ScheduledJobStatus.WAITING),
                _작업("job_남의것", "batch_1", 남, status=ScheduledJobStatus.WAITING),
            ],
        )

        응답 = await client.get("/scheduled/naver/jobs", headers=헤더)

    assert 응답.status_code == 200
    항목들 = 응답.json()["items"]
    assert [항목["job"]["jobId"] for 항목 in 항목들] == ["job_내것"]


async def test_예약_시각을_바꾸면_저장되고_워커를_깨운다():
    """시각을 앞당겼을 수 있다 — 깨우지 않으면 최대 30초 뒤에야 알아챈다."""
    환경 = _환경만들기()
    새시각 = _앞으로(7200)
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.patch(
            "/scheduled/naver/jobs/job_1",
            headers=헤더,
            json={"publishAt": 새시각, "timezone": "Asia/Seoul"},
        )

    assert 응답.status_code == 200
    저장된 = await 환경.저장소.find_job("job_1")
    assert 저장된.publish_at == 새시각
    assert 저장된.timezone == "Asia/Seoul"
    assert 환경.워커.깨운횟수 == 1


async def test_지난_시각으로_바꾸려_하면_400이다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.patch(
            "/scheduled/naver/jobs/job_1", headers=헤더, json={"publishAt": _앞으로(-7200)}
        )

    assert 응답.status_code == 400
    assert 응답.json()["errorCode"] == "VALIDATION_FAILED"
    # 거부된 요청으로 워커를 깨우지 않는다.
    assert 환경.워커.깨운횟수 == 0


async def test_남의_예약은_아이디를_알아도_바꿀_수_없다():
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        _, 주인 = await _가입하고_로그인(client, "owner@blog-it.test", "주인")
        남의헤더, _ = await _가입하고_로그인(client, "other@blog-it.test", "남")
        await 환경.저장소.create_batch(
            _배치("batch_1", 주인),
            [_작업("job_1", "batch_1", 주인, status=ScheduledJobStatus.WAITING)],
        )

        변경 = await client.patch(
            "/scheduled/naver/jobs/job_1", headers=남의헤더, json={"publishAt": _앞으로(3600)}
        )
        취소 = await client.post("/scheduled/naver/jobs/job_1/cancel", headers=남의헤더)

    assert 변경.status_code == 404
    assert 취소.status_code == 404
    assert (await 환경.저장소.find_job("job_1")).status == ScheduledJobStatus.WAITING


async def test_예약을_취소하면_상태만_바뀌고_문서는_남는다():
    """삭제(DELETE)와 다른 동작이다 — 취소는 기록을 남긴다."""
    환경 = _환경만들기()
    async with _클라이언트(환경) as client:
        헤더, 사용자 = await _가입하고_로그인(client, "writer@blog-it.test", "라이터")
        await 환경.저장소.create_batch(
            _배치("batch_1", 사용자),
            [_작업("job_1", "batch_1", 사용자, status=ScheduledJobStatus.WAITING)],
        )

        응답 = await client.post("/scheduled/naver/jobs/job_1/cancel", headers=헤더)

    assert 응답.status_code == 200
    저장된 = await 환경.저장소.find_job("job_1")
    assert 저장된 is not None
    assert 저장된.status == ScheduledJobStatus.CANCELED
    assert 환경.워커.깨운횟수 == 1
