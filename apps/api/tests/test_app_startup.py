"""기동 경로(lifespan)가 실제로 끝까지 돈다는 것을 확인한다.

왜 따로 필요한가: 나머지 테스트는 서비스 계층을 직접 만들어 쓰므로 `app.main`의 lifespan
본문을 한 줄도 실행하지 않는다. 그래서 lifespan 안에서만 참조하는 이름이 빠져 있어도
`import app.main`은 성공하고 테스트도 전부 통과한다 — 실제로 그런 일이 있었다(모듈에
import하지 않은 함수를 lifespan에서 호출해, 테스트 640개가 통과하는 동안 서버는
`NameError`로 기동조차 못 했다).

Mongo·LLM 없이 돌리기 위해 바깥 세계와 닿는 네 지점(서비스 조립·중단 작업 복구·예약
배치 복구·종료)만 가짜로 바꾼다. lifespan 본문 자체는 손대지 않으므로, 그 안에서
참조하는 이름이 하나라도 없어지면 이 테스트가 실패한다.
"""

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI

from app import main as main_module


@dataclass
class _StubService:
    shutdown_calls: list[str] = field(default_factory=list)

    async def shutdown(self) -> None:
        self.shutdown_calls.append("done")

    async def stop_orphaned_generations(self) -> int:
        """기동 때 부른다 — 서버가 꺼질 때 돌던 원고를 멈춤으로 표시한다(2026-08-12)."""
        return 0


@dataclass
class _StubWorker:
    """예약 워커. 시작·종료가 실제로 불렸는지, 어느 순서였는지 본다."""

    events: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.events.append("start")

    def wake(self) -> None:
        self.events.append("wake")

    async def shutdown(self) -> None:
        self.events.append("shutdown")


@dataclass
class _StubServices:
    storage_status: str = "stub"
    shared_state_status: str = "메모리"
    llm_status: list = field(default_factory=list)
    blog_task_service: _StubService = field(default_factory=_StubService)
    draft_service: _StubService = field(default_factory=_StubService)
    # 키워드 선행 수집도 lifespan이 정리한다(2026-08-07).
    trend_service: _StubService = field(default_factory=_StubService)
    scheduled_posting_worker: _StubWorker = field(default_factory=_StubWorker)
    scheduled_posting_repository: object | None = None


@pytest.fixture
def stubbed_lifespan(monkeypatch):
    services = _StubServices()
    closed: list[str] = []
    temp_cleanup: list[str] = []

    async def fake_create():
        return services

    async def fake_recover(_services):
        return 0

    async def fake_shutdown(_services):
        closed.append("services")

    monkeypatch.setattr(main_module, "create_runtime_services", fake_create)
    monkeypatch.setattr(main_module, "initialize_signing_secret", lambda: b"test-secret")
    monkeypatch.setattr(main_module, "recover_interrupted_jobs", fake_recover)
    monkeypatch.setattr(main_module, "recover_scheduled_posting", fake_recover)
    monkeypatch.setattr(main_module, "shutdown_services", fake_shutdown)
    monkeypatch.setattr(
        main_module,
        "cleanup_stale_thread_image_dirs",
        lambda: temp_cleanup.append("threads") or 0,
    )
    return services, closed, temp_cleanup


async def test_the_lifespan_runs_start_to_finish(stubbed_lifespan):
    services, closed, temp_cleanup = stubbed_lifespan
    app = main_module.create_app()

    async with main_module.lifespan(app):
        # 기동이 끝나면 라우트가 서비스를 여기서 찾는다. 비어 있으면 모든 요청이 죽는다.
        assert app.state.services is services

    # 종료 순서: 잡을 먼저 정리하고 그다음 연결을 닫는다(닫힌 연결에 쓰다 죽지 않도록).
    assert services.blog_task_service.shutdown_calls == ["done"]
    assert services.draft_service.shutdown_calls == ["done"]
    assert closed == ["services"]

    # 예약 워커는 기동에서 뜨고 종료에서 내려간다. 뜨지 않으면 예약이 영영 실행되지
    # 않고, 내려가지 않으면 정리된 서비스로 다음 작업을 집어 간다.
    assert services.scheduled_posting_worker.events == ["start", "shutdown"]
    assert temp_cleanup == ["threads"]


@pytest.mark.parametrize("configured", [None, "too-short"])
async def test_production_rejects_a_missing_or_short_auth_secret_before_services_start(
    monkeypatch, configured
):
    from app.modules.auth import token as token_module

    monkeypatch.setattr(main_module, "load_env_file", lambda: True)
    monkeypatch.setenv("APP_ENV", "production")
    if configured is None:
        monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
    else:
        monkeypatch.setenv("AUTH_TOKEN_SECRET", configured)
    monkeypatch.setattr(token_module, "_SECRET", None)

    started = False

    async def should_not_start():
        nonlocal started
        started = True
        return _StubServices()

    monkeypatch.setattr(main_module, "create_runtime_services", should_not_start)

    with pytest.raises(RuntimeError, match="AUTH_TOKEN_SECRET"):
        async with main_module.lifespan(FastAPI()):
            pass
    assert started is False
