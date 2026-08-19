"""FastAPI 앱: 라우트, 에러 핸들러, 빌드된 프론트엔드 서빙."""

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import cors_allowed_origins, load_env_file, port, web_dir
from app.errors import AuthError, BlogTaskError, InvalidUserSettingsError
from app.http import routes
from app.http.responses import (
    auth_error_handler,
    blog_task_error_handler,
    error_response,
    fallback_error_handler,
    invalid_settings_handler,
)
from app.llm import describe_llm_status
from app.modules.auth.token import initialize_signing_secret
from app.posting.threads_browser import cleanup_stale_thread_image_dirs
from app.services import (
    create_runtime_services,
    recover_interrupted_jobs,
    recover_scheduled_posting,
    shutdown_services,
)


# 화면이 몇 초마다 두드리는 폴링 경로. 성공 access 로그를 버릴 대상이다.
_POLLING_ACCESS_PATH = re.compile(r"^/(?:posting/verification|posts/[^/]+/status)$")


def _drop_polling_access_logs(record: logging.LogRecord) -> bool:
    """성공한 폴링 요청의 uvicorn access 한 줄을 버린다(False = 버림).

    uvicorn.access의 레코드 인자는 (클라이언트, 메서드, 경로, HTTP 버전, 상태) 5칸이다.
    형태가 다르면(uvicorn 내부 변경) 아무것도 거르지 않는다 — 로그를 잃는 것보다
    시끄러운 쪽이 안전하다. 실패(4xx·5xx) 폴링은 계속 보인다.
    """
    args = record.args
    if not isinstance(args, tuple) or len(args) != 5:
        return True
    _client, method, path, _version, status = args
    try:
        succeeded = 200 <= int(status) < 300
    except (TypeError, ValueError):
        return True
    plain_path = str(path).partition("?")[0]
    return not (
        succeeded
        and str(method).upper() == "GET"
        and _POLLING_ACCESS_PATH.match(plain_path) is not None
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env_file()
    # 운영 비밀은 첫 로그인 때가 아니라 기동 단계에서 검증한다. 그렇지 않으면 회원 문서를
    # 먼저 만든 뒤 세션 서명에서 실패해, 사용자는 500을 보지만 계정은 생긴 부분 상태가 된다.
    initialize_signing_secret()

    # uvicorn은 자기 로거만 설정하고 그 외에는 손대지 않아서, 앱이 남긴 로그는
    # 아무 데도 가지 않았다. 트렌드 캐시는 풀에서 내줬는지 API 호출을 썼는지 알려주는데,
    # 그건 볼 수 있어야 쓸모가 있다.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:     %(message)s",
    )

    # httpx는 모든 요청을 INFO로 로깅한다 — 전체 URL을 포함하는데, SerpApi와
    # YouTube는 쿼리 문자열에 API 키를 담는다. 앱 로깅을 켜면 이것도 함께 켜져서
    # 키가 터미널에 찍혔다. 키는 로그 파일에 남기지 않는다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # 화면 폴링(status·발행 검증)은 몇 초마다 오가서 성공 줄이 터미널을 도배한다
    # (2026-08-10 사용자: "로그가 되게 기네?"). 성공(2xx) 폴링만 걸러낸다 — 실패는
    # 그대로 보인다. 다른 요청의 access 로그도 그대로다.
    logging.getLogger("uvicorn.access").addFilter(_drop_polling_access_logs)

    # 비정상 종료 때 TEMP에 남은 Threads 평문 이미지 복사본은 다음 발행을 기다리지 않고
    # 앱 기동 시 정리한다. 함수 자체가 OS temp 바로 아래의 전용 prefix와 24시간 cutoff만
    # 허용하므로 다른 디렉터리를 건드리지 않는다.
    cleanup_stale_thread_image_dirs()

    services = await create_runtime_services()
    app.state.services = services

    # flush: stdout은 터미널이 아닐 때(docker logs, 리다이렉트된 로그 파일) 항상
    # 블록 버퍼링된다. 이 줄들은 시작 시점에 어떤 저장소와 어떤 provider로 정해졌는지
    # 알려주기 위해 존재한다.
    print(f"저장소: {services.storage_status}", flush=True)
    # 여러 프로세스가 나눠 보는 상태(트렌드 노출 이력·잡 임차)가 어디 있는지.
    print(f"공유 상태: {services.shared_state_status}", flush=True)

    # 앞선 프로세스가 원고·검증을 돌리던 중에 죽었으면 그 글은 '진행 중'인 채로 남아
    # 화면의 스피너가 영영 돈다. 시작할 때 한 번 훑어 되살린다.
    recovered = await recover_interrupted_jobs(services)
    if recovered:
        print(f"중단된 작업 {recovered}건을 되살렸습니다.", flush=True)

    # 예약 배치도 같은 이유로 정리한다 — 글 복구가 끝난 뒤라야 예약 작업이 보는
    # BlogTask 상태가 확정돼 있다. 정리가 끝나면 워커를 띄워 남은 작업을 이어 간다.
    # 서버가 꺼질 때 돌던 원고 생성은 **이어받지 않는다**(2026-08-12 사용자 지시).
    # 멈춤으로 표시해 두면 화면이 '다시 생성하기'를 주고, 사람이 눌러야 다시 돈다.
    stopped = await services.blog_task_service.stop_orphaned_generations()
    if stopped:
        print(f"서버가 꺼질 때 돌던 원고 {stopped}건을 멈춤으로 표시했습니다.", flush=True)

    resumed = await recover_scheduled_posting(services)
    if resumed:
        print(f"예약 작업 {resumed}건을 이어서 진행합니다.", flush=True)
    services.scheduled_posting_worker.start()

    print("기능별 사용 모델:", flush=True)
    for line in describe_llm_status(services.llm_status):
        print(line, flush=True)

    yield

    # 진행 중인 백그라운드 잡을 먼저 정리하고(우아한 종료), 그다음 외부 연결을 닫는다 —
    # 순서를 바꾸면 잡이 닫힌 DB·연결 풀에 쓰다 죽는다.
    # 예약 워커를 가장 먼저 멈춘다. 이 워커가 아래 두 서비스를 부르므로, 나중에 세우면
    # 이미 정리된 서비스로 다음 작업을 시작할 수 있다.
    await services.scheduled_posting_worker.shutdown()
    await services.blog_task_service.shutdown()
    await services.draft_service.shutdown()
    # 키워드 선행 수집도 같은 자리에서 정리한다 — 잃을 것이 없는 가속 장치라 취소해도
    # 다음 요청이 다시 모은다.
    await services.trend_service.shutdown()
    await shutdown_services(services)
    # provider 호출이 공유하는 keep-alive 연결 풀 정리.
    from app.llm.http import close_shared_client

    await close_shared_client()



def create_app() -> FastAPI:
    # CORS 같은 앱 생성 시점 설정도 .env 값을 보게 한다. 실제 환경 변수는 덮어쓰지 않는다.
    load_env_file()
    app = FastAPI(title="Blog-it API", lifespan=lifespan, docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins(),
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
        max_age=86400,
    )

    app.add_exception_handler(BlogTaskError, blog_task_error_handler)
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(InvalidUserSettingsError, invalid_settings_handler)
    app.add_exception_handler(
        routes._RequestBodyTooLarge, routes.request_body_too_large_handler
    )
    # 잘못된 JSON은 400, 그 밖의 예상하지 못한 예외는 상세를 숨긴 500으로 응답한다.
    app.add_exception_handler(Exception, fallback_error_handler)

    app.include_router(routes.router)

    static_root = web_dir()
    if static_root.is_dir():

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(static_root / "index.html")

        app.mount("/", StaticFiles(directory=static_root), name="static")
    else:

        @app.get("/{path:path}", include_in_schema=False)
        async def not_found(path: str) -> JSONResponse:
            return error_response(404, "ROUTE_NOT_FOUND", "no matching route")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port())
