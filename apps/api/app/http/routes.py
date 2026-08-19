"""HTTP 라우트.

경로, 상태 코드, 응답 형태가 TypeScript 서버와 정확히 일치해서, 기존 프런트엔드가
어느 쪽에 붙어도 수정 없이 동작한다.
"""

import base64
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.errors import AuthError, BlogTaskError
from app.llm.prompts import split_data_url
from app.modules.blog_task.validation import validate_additional_drafts
from app.modules.brand import (
    evaluate_brand_fit,
    fit_context_of,
    with_brand_materials,
)
from app.posting.config import naver_config_from_env, naver_profile_dir, remember_blog_id
from app.posting.credentials import (
    NaverCredentials,
    load_credentials,
    save_credentials,
    saved_username,
    session_account,
)
from app.services import ApiServices
from app.shared import BlogTaskStatus, PublicUser

from .responses import bare, envelope, error_response

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_JSON_BODY_BYTES = 16 * 1024 * 1024


def _services(request: Request) -> ApiServices:
    return request.app.state.services


async def _json_body(request: Request) -> Any:
    """빈 본문은 Node 서버에서처럼 {} 로 읽는다."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY_BYTES:
            raise _RequestBodyTooLarge()
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    import json

    return json.loads(raw)


async def _authenticate(request: Request) -> PublicUser:
    return await _services(request).auth_service.authenticate(
        request.headers.get("authorization")
    )


def _assert_self(user: PublicUser, user_id: str) -> str:
    if user.user_id != user_id:
        raise AuthError("FORBIDDEN", "you can only access your own resources")
    return user_id


async def _authorize_post(request: Request, post_id: str) -> PublicUser:
    """이 글이 이 사람의 것인지 확인한다. **글 내용은 읽지 않는다.**

    예전에는 여기서 글을 통째로 읽고 존재 여부만 본 뒤 버렸다. 이 앱의 글 문서에는 카드
    이미지가 base64로 들어 있고 원고가 두 벌 저장되므로 한 편이 1~4MB인데, 그것이
    **/posts 아래 모든 동작의 앞에** 붙어 있었다(10개 라우트). 곧이어 핸들러가 같은 글을
    다시 읽으므로 그 왕복은 통째로 낭비였고, 삭제처럼 내용이 필요 없는 동작에서는 더
    그랬다 — '선택 삭제'로 24편을 한 번에 지우면 수십 MB가 오가느라 끝나지 않았다
    (2026-08-06 신고).

    응답은 그대로다: 없는 글도 남의 글도 404다(소유자 조건이 쿼리 안에 있다).
    """
    user = await _authenticate(request)
    owned = await _services(request).blog_task_service.user_owns_blog_task(
        user.user_id, post_id
    )
    if not owned:
        raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
    return user


class _RequestBodyTooLarge(Exception):
    pass


async def request_body_too_large_handler(
    _request: Request, _error: _RequestBodyTooLarge
) -> JSONResponse:
    return error_response(413, "REQUEST_TOO_LARGE", "요청 본문은 최대 16MB까지 허용됩니다.")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    return bare(
        {
            "ok": True,
            "service": "api",
            "firstStatus": BlogTaskStatus.INPUT.value,
            "storageStatus": _services(request).storage_status,
            # 노출 이력·멱등성 키가 프로세스 바깥(Redis)에 있는지. 여러 대로 늘렸을 때
            # 상태가 공유되는지를 배포 직후 확인할 수 있는 유일한 자리다.
            "sharedStateStatus": _services(request).shared_state_status,
        }
    )


# --- auth: responses are bare objects, not enveloped ---


@router.post("/auth/signup", status_code=201)
async def signup(request: Request) -> JSONResponse:
    client_key = request.client.host if request.client else "unknown"
    session = await _services(request).auth_service.sign_up(
        await _json_body(request), client_key=client_key
    )
    response = bare(session, 201)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/login")
async def login(request: Request) -> JSONResponse:
    client_key = request.client.host if request.client else "unknown"
    session = await _services(request).auth_service.log_in(
        await _json_body(request), client_key=client_key
    )
    response = bare(session)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/me")
async def current_user(request: Request) -> JSONResponse:
    response = bare(await _authenticate(request))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> JSONResponse:
    await _services(request).auth_service.log_out(request.headers.get("authorization"))
    return JSONResponse(None, status_code=204, headers={"Cache-Control": "no-store"})


# --- persona catalog and user settings: also bare ---


@router.get("/personas")
async def get_personas(request: Request) -> JSONResponse:
    """프론트가 표시할 공용 프리셋과 커스텀 입력 선택 항목."""
    return bare(await _services(request).persona_service.list_catalog())


@router.get("/users/{user_id}/settings")
async def get_settings(request: Request, user_id: str) -> JSONResponse:
    user = await _authenticate(request)
    _assert_self(user, user_id)

    settings = await _services(request).user_settings_service.get_by_user_id(user_id)
    if settings is None:
        return error_response(404, "NOT_FOUND", "user settings not found")
    return bare(settings)


@router.put("/users/{user_id}/settings")
async def put_settings(request: Request, user_id: str) -> JSONResponse:
    from app.modules.user_settings.validation import parse_upsert_user_settings_body

    user = await _authenticate(request)
    _assert_self(user, user_id)

    body = await _json_body(request)
    settings = await _services(request).user_settings_service.save(
        parse_upsert_user_settings_body(body, user_id)
    )
    return bare(settings)


# --- 브랜드 자료: 글마다 반복해서 들어가는 회사·서비스 정보 ---
#
# 네이버 로그인 정보와 달리 **DB에 저장한다.** 비밀이 아니고, 사용자별로 나뉘어야 하며,
# 나중에 다른 사용자와 나눠 쓸 계획이라 문서로 서 있어야 한다.


@router.get("/brands/audience-options")
async def brand_audience_options(request: Request) -> JSONResponse:
    """주요 고객 선택지(대분류 → 유형).

    화면이 목록을 따로 들고 있으면 서버 검증과 어긋나 사용자가 고칠 수 없는 오류가 난다.
    한곳(``AUDIENCE_CATALOG``)에서 내려 준다.
    """
    from app.shared import AUDIENCE_CATALOG, AUDIENCE_OTHER

    await _authenticate(request)
    return bare(
        {
            "otherLabel": AUDIENCE_OTHER,
            "categories": [
                {"category": category, "types": list(types)}
                for category, types in AUDIENCE_CATALOG.items()
            ],
        }
    )


@router.get("/brands")
async def list_brands(
    request: Request, view: Literal["full", "summary"] = "full"
) -> JSONResponse:
    """브랜드 목록.

    ``view=summary``는 **이미지·문서의 base64를 뺀다.** 브랜드 하나가 2MB인데(실측:
    이미지 9장) 고르기 화면은 이름과 한 줄 소개만 그린다 — 그걸 보여 주려고 2MB를
    기다리느라 화면이 "브랜드 자료를 불러오는 중입니다"에 오래 머물렀다.

    query가 없는 기존 호출은 전체 응답을 그대로 유지한다(`/posts`와 같은 규칙).
    """
    user = await _authenticate(request)
    service = _services(request).brand_service
    if view == "summary":
        return bare([b.to_wire() for b in await service.list_brand_items(user.user_id)])
    return bare([b.to_wire() for b in await service.list_brands(user.user_id)])


@router.post("/brands", status_code=201)
async def create_brand(request: Request) -> JSONResponse:
    user = await _authenticate(request)
    body = await _json_body(request)
    brand = await _services(request).brand_service.create_brand(user.user_id, body)
    return bare(brand.to_wire(), 201)


@router.get("/brands/{brand_id}")
async def get_brand(request: Request, brand_id: str) -> JSONResponse:
    """``?view=light``면 이미지·문서 base64를 뺀 텍스트 필드만 돌려준다.

    전체 문서는 2MB라 Atlas에서 20초 넘게 걸린다(2026-08-07 실측) — 자료 편집 화면이
    가벼운 것으로 먼저 열리고 첨부는 전체 조회가 뒤따라 채운다.
    """
    user = await _authenticate(request)
    service = _services(request).brand_service
    if request.query_params.get("view") == "light":
        brand = await service.get_brand_light(user.user_id, brand_id)
    else:
        brand = await service.get_brand(user.user_id, brand_id)
    return bare(brand.to_wire())


@router.put("/brands/{brand_id}")
async def update_brand(request: Request, brand_id: str) -> JSONResponse:
    user = await _authenticate(request)
    body = await _json_body(request)
    brand = await _services(request).brand_service.update_brand(user.user_id, brand_id, body)
    return bare(brand.to_wire())


@router.post("/brands/{brand_id}/fit")
async def check_brand_fit(request: Request, brand_id: str) -> JSONResponse:
    """이 소재에 이 브랜드를 얹는 것이 자연스러운가 — A·B·C(2026-08-19).

    저장할 때도 같은 판정을 하지만(`with_brand_materials`), 화면은 **저장하기 전에**
    알아야 한다. 억지 조합(C)으로 글을 만들면 되돌릴 수 있는 것은 원고 한 편을 다 만든
    뒤이고, 그 원고는 광고 문장으로 채워져 있다. 소재를 적는 자리에서 미리 알려 주면
    사용자는 소재를 바꾸거나 브랜드를 빼는 쪽을 고를 수 있다.

    글을 만들지 않으므로 저장도 하지 않는다 — 순수한 조회다.
    """
    user = await _authenticate(request)
    body = await _json_body(request)
    profile = await _services(request).brand_service.get_brand(user.user_id, brand_id)
    topic = body.get("topic")
    fit = evaluate_brand_fit(
        profile,
        topic if isinstance(topic, str) else "",
        context=fit_context_of(body),
    )
    return bare(fit.to_wire())


# 브랜드 전용 글 생성 경로(`POST /brands/{id}/posts`와 `.../posts/auto`)는 2026-08-11에
# 없앴다. 브랜드는 이제 별도 화면이 아니라 **새 글 작성 소재 단계의 선택 항목**이고,
# 글은 `POST /posts`가 만들고, 브랜드 자료는 `with_brand_materials`가 얹는다
# (자동 포스팅의 예약 작업도 같은 함수를 쓴다).


@router.delete("/brands/{brand_id}", status_code=204)
async def delete_brand(request: Request, brand_id: str) -> JSONResponse:
    user = await _authenticate(request)
    await _services(request).brand_service.delete_brand(user.user_id, brand_id)
    return JSONResponse(None, status_code=204)


# --- 네이버 로그인 정보: 로그인 사용자별 로컬 파일, DB에는 절대 저장하지 않는다 ---


@router.get("/naver/status")
async def naver_status(request: Request) -> JSONResponse:
    user = await _authenticate(request)

    profile_dir = naver_profile_dir(user.user_id)
    username = saved_username(profile_dir)
    config = (
        naver_config_from_env(username=username, user_id=user.user_id) if username else None
    )
    return bare(
        {
            "configured": config is not None,
            "blogId": config.blog_id if config else None,
            "saved": username is not None,
            # 어느 계정이 기억됐는지 사용자가 볼 수 있도록 아이디는 노출한다.
            # 비밀번호는 여기서 읽을 수 없고, 반환하는 라우트도 없다.
            "savedUsername": username,
            # 저장된 네이버 세션이 있으면 발행 때 로그인창이 뜨지 않는다.
            "hasSession": config.has_session if config else False,
            # 어느 계정으로 로그인돼 있는지. 화면 배지가 이걸 그대로 보여준다.
            "sessionAccount": session_account(profile_dir),
        }
    )


@router.post("/naver/save")
async def save_naver_credentials(request: Request) -> JSONResponse:
    """현재 Blog-it 사용자의 네이버 로그인 정보를 DB 밖 로컬 파일에 저장한다."""
    user = await _authenticate(request)
    profile_dir = naver_profile_dir(user.user_id)

    body = await _json_body(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    # 비밀번호를 비워 다시 저장하면 현재 사용자가 이전에 저장한 값만 재사용한다.
    remembered = load_credentials(profile_dir)
    if not password and remembered and (not username or username == remembered.username):
        username, password = remembered.username, remembered.password

    if not username or not password:
        return error_response(400, "VALIDATION_FAILED", "네이버 아이디와 비밀번호가 필요합니다.")

    # 블로그 주소는 묻지 않는다: 네이버 블로그는 blog.naver.com/<아이디> 에 있다.
    config = naver_config_from_env(
        username=username,
        password=password,
        user_id=user.user_id,
    )
    if config is None:
        return error_response(400, "VALIDATION_FAILED", "네이버 설정을 만들 수 없습니다.")

    remember_blog_id(profile_dir, config.blog_id)
    save_credentials(profile_dir, NaverCredentials(username, password))

    return bare(
        {
            "configured": True,
            "saved": True,
            "blogId": config.blog_id,
            "savedUsername": username,
            "hasSession": config.has_session,
            "sessionAccount": session_account(profile_dir),
        }
    )


@router.post("/naver/login")
async def naver_login(request: Request) -> JSONResponse:
    """네이버에 한 번 로그인해 이 PC 프로필에 세션을 저장한다.

    성공하면 이후 발행할 때 로그인창이 뜨지 않고 바로 글쓰기 화면으로 넘어간다. 열린
    네이버 창에서 캡차·2단계 인증이 필요하면 사용자가 직접 처리한다. 입력한 아이디·
    비밀번호가 있으면 로그인과 함께 저장해 두어 다음 로그인에 재사용한다.
    """
    from app.posting.naver import _NeedsHuman, log_in_and_store_session

    user = await _authenticate(request)
    profile_dir = naver_profile_dir(user.user_id)

    body = await _json_body(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    # 비밀번호를 비우고 눌렀다면 현재 사용자가 저장해 둔 값을 재사용한다.
    remembered = load_credentials(profile_dir)
    if not password and remembered and (not username or username == remembered.username):
        username, password = remembered.username, remembered.password
    if not username:
        username = saved_username(profile_dir) or ""

    config = naver_config_from_env(
        username=username or None,
        password=password or None,
        user_id=user.user_id,
    )
    if config is None:
        return error_response(
            400, "VALIDATION_FAILED", "먼저 네이버 아이디를 입력하거나 저장해 주세요."
        )

    # 아이디·비밀번호를 새로 입력했다면 로그인과 함께 저장해 둔다.
    if username and password:
        remember_blog_id(profile_dir, config.blog_id)
        save_credentials(profile_dir, NaverCredentials(username, password))

    try:
        await log_in_and_store_session(config)
    except _NeedsHuman as error:
        return error_response(409, "NAVER_NEEDS_HUMAN", str(error))
    except Exception as error:
        # 브라우저 예외에는 현재 URL/OAuth query/로컬 경로가 섞일 수 있다. 응답과 로그에는
        # 예외 형식만 남기고 계정·토큰 후보 문자열은 싣지 않는다.
        logger.warning("네이버 로그인 실패 (%s)", type(error).__name__)
        return error_response(502, "NAVER_LOGIN_FAILED", "네이버 로그인에 실패했습니다.")

    return bare(
        {
            "configured": True,
            "saved": saved_username(profile_dir) is not None,
            "blogId": config.blog_id,
            "savedUsername": saved_username(profile_dir),
            "hasSession": config.has_session,
            "sessionAccount": session_account(profile_dir),
        }
    )


# --- 스레드 로그인 정보: 네이버와 같은 원칙 — 사용자별 로컬 파일, DB에는 절대 저장하지 않는다 ---


@router.get("/threads/status")
async def threads_status(request: Request) -> JSONResponse:
    from app.posting.threads_browser import has_threads_session, threads_profile_dir

    user = await _authenticate(request)
    profile_dir = threads_profile_dir(user.user_id)
    username = saved_username(profile_dir)
    return bare(
        {
            "saved": username is not None,
            # 어느 계정이 기억됐는지는 보여준다. 비밀번호를 돌려주는 라우트는 없다.
            "savedUsername": username,
            # 세션이 있으면 발행 때 로그인창 없이 바로 게시된다.
            "hasSession": has_threads_session(profile_dir),
            "sessionAccount": session_account(profile_dir),
        }
    )


@router.post("/threads/save")
async def save_threads_credentials(request: Request) -> JSONResponse:
    """현재 Blog-it 사용자의 스레드 로그인 정보를 DB 밖 로컬 파일에 저장한다.

    아이디는 스레드 로그인 방식 그대로 사용자 이름·전화번호·이메일 중 하나다.
    발행 때 자동 입력에 쓰이고, Meta가 추가 확인을 요구하면 열린 창에서 사람이 처리한다.
    """
    from app.posting.threads_browser import has_threads_session, threads_profile_dir

    user = await _authenticate(request)
    profile_dir = threads_profile_dir(user.user_id)

    body = await _json_body(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    # 비밀번호를 비워 다시 저장하면 현재 사용자가 이전에 저장한 값만 재사용한다.
    remembered = load_credentials(profile_dir)
    if not password and remembered and (not username or username == remembered.username):
        username, password = remembered.username, remembered.password

    if not username or not password:
        return error_response(
            400, "VALIDATION_FAILED", "스레드 아이디(사용자 이름·전화번호·이메일)와 비밀번호가 필요합니다."
        )

    save_credentials(profile_dir, NaverCredentials(username, password))

    return bare(
        {
            "saved": True,
            "savedUsername": username,
            "hasSession": has_threads_session(profile_dir),
            "sessionAccount": session_account(profile_dir),
        }
    )


@router.post("/threads/login")
async def threads_login(request: Request) -> JSONResponse:
    """스레드에 한 번 로그인해 이 PC 프로필에 세션을 저장한다.

    발행 도중에 2단계 인증을 만나면 그 발행이 멈춘다. 설정 화면에서 미리 눌러 두면
    사람이 앉아 있을 때 인증을 끝낼 수 있다. 네이버의 ``/naver/login``과 같은 자리다.
    입력한 아이디·비밀번호가 있으면 로그인과 함께 저장해 다음에 재사용한다.
    """
    from app.posting.naver import _NeedsHuman
    from app.posting.threads_browser import (
        has_threads_session,
        log_in_and_store_threads_session,
        threads_profile_dir,
    )

    user = await _authenticate(request)
    profile_dir = threads_profile_dir(user.user_id)

    body = await _json_body(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    # 비밀번호를 비우고 눌렀다면 저장해 둔 값을 재사용한다(네이버 로그인과 같은 규칙).
    remembered = load_credentials(profile_dir)
    if not password and remembered and (not username or username == remembered.username):
        username, password = remembered.username, remembered.password
    if not username:
        username = saved_username(profile_dir) or ""
    if not username or not password:
        return error_response(
            400,
            "VALIDATION_FAILED",
            "먼저 스레드 아이디와 비밀번호를 입력하거나 저장해 주세요.",
        )

    save_credentials(profile_dir, NaverCredentials(username, password))

    try:
        # user_id를 넘겨야 로그인 크롬 화면이 라이브 뷰로 중계된다 — 외부 PC 사용자가
        # 2단계 인증 화면을 보는 유일한 길이다.
        await log_in_and_store_threads_session(profile_dir, user_id=user.user_id)
    except _NeedsHuman as error:
        return error_response(409, "THREADS_NEEDS_HUMAN", str(error))
    except Exception as error:
        logger.warning("스레드 로그인 실패 (%s)", type(error).__name__)
        return error_response(502, "THREADS_LOGIN_FAILED", "스레드 로그인에 실패했습니다.")

    return bare(
        {
            "saved": True,
            "savedUsername": saved_username(profile_dir),
            "hasSession": has_threads_session(profile_dir),
            "sessionAccount": session_account(profile_dir),
        }
    )


# --- 라이브 뷰: 서버에서 도는 발행 크롬 화면을 웹으로 중계·조작 ---
#
# 발행·로그인 크롬은 서버 PC에 뜬다. 외부 PC의 사용자는 이 라우트로 그 화면을 보고
# (SSE로 JPEG 프레임), 클릭·키보드를 넣는다(CDP Input) — 2단계 인증·캡차를 어디서든
# 처리할 수 있다. 세션은 발행·로그인 코드가 브라우저를 열 때 등록한다(posting/live_view).


@router.get("/live/sessions")
async def live_sessions(request: Request) -> JSONResponse:
    """지금 이 사용자 몫으로 중계 중인 크롬 화면 목록."""
    from app.posting.live_view import hub

    user = await _authenticate(request)
    response = bare({"sessions": hub.list_for_user(user.user_id)})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/live/{channel}/stream")
async def live_stream(request: Request, channel: str):
    """화면 프레임 SSE 스트림. 브라우저 EventSource는 인증 헤더를 못 실으므로
    프런트는 fetch 스트리밍으로 읽는다."""
    from fastapi.responses import StreamingResponse

    from app.posting.live_view import MAX_STREAM_SUBSCRIBERS, frame_stream, hub

    user = await _authenticate(request)
    session = hub.get(user.user_id, channel)
    if session is None:
        return error_response(404, "NOT_FOUND", "지금 중계 중인 화면이 없습니다.")
    # SSE는 시청자마다 서버 스레드풀 토큰을 상시 점유한다 — 같은 화면을 여러 탭이
    # 무제한으로 열면 라이브 뷰가 자기 부하로 다른 요청을 굶긴다.
    if session.subscriber_count >= MAX_STREAM_SUBSCRIBERS:
        return error_response(
            409, "LIVE_VIEW_BUSY", "이 화면은 이미 다른 탭에서 보고 있습니다."
        )
    return StreamingResponse(
        frame_stream(session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # nginx가 이 응답을 버퍼링하면 프레임이 한참 뒤에 몰려 도착한다 — 응답
            # 단위로 버퍼링을 끈다(서버 설정을 따로 만지지 않아도 되게).
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/live/{channel}/input")
async def live_input(request: Request, channel: str) -> JSONResponse:
    """사용자의 클릭·키보드를 중계 중인 크롬에 넣는다."""
    from app.posting.live_view import LiveViewError, hub

    user = await _authenticate(request)
    session = hub.get(user.user_id, channel)
    if session is None:
        return error_response(404, "NOT_FOUND", "지금 중계 중인 화면이 없습니다.")
    body = await _json_body(request)
    events = body.get("events")
    if not isinstance(events, list) or not events:
        return error_response(400, "VALIDATION_FAILED", "전달할 입력 이벤트가 없습니다.")
    try:
        # 동기 웹소켓 send다 — 이벤트 루프에서 직접 부르면 크롬이 소켓을 못 읽는
        # 순간(행업·버퍼 포화) 서버의 **모든** 요청이 최대 30초 함께 멈춘다.
        from starlette.concurrency import run_in_threadpool

        handled = await run_in_threadpool(
            session.dispatch_input, [e for e in events if isinstance(e, dict)][:64]
        )
    except LiveViewError as error:
        logger.info("라이브 뷰 입력 거부(%s): %s", channel, error)
        return error_response(409, "LIVE_VIEW_UNAVAILABLE", str(error))
    # 입력이 서버까지 왔는지 백엔드 콘솔에서 바로 볼 수 있게 남긴다(입력 한 묶음 = 한 줄).
    logger.info("라이브 뷰 입력 전달(%s): %d건", channel, handled)
    return bare({"handled": handled})


# --- 2단계 인증 코드 창구 ---
#
# 발행 자동화가 2단계 인증 화면을 만나면 코드를 기다리며 멈춘다(posting/verification.py).
# 화면은 아래 세 라우트로 그 대기를 보고, 코드를 넣고, 필요하면 취소한다. 코드는 메모리에만
# 잠깐 머물다 자동화로 넘어가고 어디에도 저장되지 않는다.


@router.get("/posting/verification")
async def posting_verification(request: Request) -> JSONResponse:
    """지금 이 사용자에게 물어보는 인증코드 요청이 있으면 그 내용."""
    from app.posting.verification import broker

    user = await _authenticate(request)
    pending = broker.pending(user.user_id)
    return bare({"pending": pending.as_dict() if pending is not None else None})


@router.post("/posting/verification")
async def submit_posting_verification(request: Request) -> JSONResponse:
    """사용자가 받은 인증코드를 기다리던 자동화에 넘긴다."""
    from app.posting.verification import broker

    user = await _authenticate(request)
    body = await _json_body(request)
    # 문자 메시지를 그대로 붙여넣는 경우가 많아 공백·하이픈은 흘려보낸다.
    code = "".join((body.get("code") or "").split()).replace("-", "")
    if not code:
        return error_response(400, "VALIDATION_FAILED", "인증코드를 입력해 주세요.")

    if not broker.submit(user.user_id, code):
        return error_response(
            409, "NO_PENDING_VERIFICATION", "지금 입력을 기다리는 인증 요청이 없습니다."
        )
    return bare({"accepted": True})


@router.delete("/posting/verification")
async def cancel_posting_verification(request: Request) -> JSONResponse:
    """사용자가 입력을 포기했다. 자동화는 곧바로 실패로 끝난다."""
    from app.posting.verification import broker

    user = await _authenticate(request)
    return bare({"cancelled": broker.cancel(user.user_id)})


# --- posts: every response is {success, data} ---


@router.get("/posts")
async def list_posts(
    request: Request, view: Literal["full", "summary"] = "full"
) -> JSONResponse:
    user = await _authenticate(request)
    # 클라이언트는 소유자 범위를 고를 수 없다. 생성이든 조회든 userId의 유일한 출처는
    # 인증된 토큰이다.
    if view == "summary":
        tasks = await _services(request).blog_task_service.list_blog_task_items(user.user_id)
    else:
        # query가 없는 기존 호출은 전체 BlogTask 응답을 그대로 유지한다.
        tasks = await _services(request).blog_task_service.list_blog_tasks(user.user_id)
    return envelope(tasks)


# 브랜드 자료를 글 입력에 얹는 일은 **브랜드 모듈**이 한다(`with_brand_materials`).
# 예전에는 이 파일에 있었는데, 자동 포스팅의 예약 작업도 같은 일을 해야 해서(글을 만드는
# 통로가 라우트 하나가 아니다) 두 벌이 될 뻔했다 — 그러면 화면으로 만든 글과 예약으로
# 만든 글의 브랜드 처리가 조용히 갈라진다.


@router.post("/posts", status_code=201)
async def create_post(request: Request) -> JSONResponse:
    user = await _authenticate(request)
    body = await _json_body(request)
    # 새 태스크의 소유자는 요청 본문이 아니라 토큰이 정한다.
    services = _services(request)
    payload, limit = await with_brand_materials(
        _services(request).brand_service, user.user_id, body
    )
    task = await services.blog_task_service.create_blog_task(
        {**payload, "userId": user.user_id}, limit
    )
    # 소재·참고자료가 저장된 순간 제목 단계에 필요한 것은 이미 다 있다. 화면이 뜨기를
    # 기다리지 말고 키워드를 미리 모은다(select_intent가 콘텐츠 설계를 선행하는 것과 같은
    # 자리다). 실패해도 저장에는 영향이 없고, 화면의 요청이 어차피 다시 모은다.
    services.trend_service.start_keyword_prefetch(task)
    return envelope(task, 201)


@router.get("/posts/{post_id}/status")
async def get_post_status(request: Request, post_id: str) -> JSONResponse:
    user = await _authenticate(request)
    snapshot = await _services(request).blog_task_service.get_user_blog_task_status(
        user.user_id, post_id
    )
    if snapshot is None:
        return error_response(404, "NOT_FOUND", "blog task not found")
    response = envelope(snapshot)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/posts/{post_id}")
async def get_post(request: Request, post_id: str) -> JSONResponse:
    user = await _authenticate(request)
    task = await _services(request).blog_task_service.get_user_blog_task(
        user.user_id, post_id
    )
    if task is None:
        return error_response(404, "NOT_FOUND", "blog task not found")
    return envelope(task)


@router.delete("/posts/{post_id}", status_code=204)
async def delete_post(request: Request, post_id: str) -> Response:
    # 없는 글과 남의 글은 둘 다 404 뒤에 숨긴다. 일괄/전체 삭제는 클라이언트가 글마다
    # 이걸 한 번씩 호출하는 것이다.
    user = await _authorize_post(request, post_id)
    services = _services(request)
    await services.blog_task_service.delete_user_blog_task(user.user_id, post_id)
    # 그 글을 만든 예약 기록도 함께 없앤다. 남겨 두면 발행 내역이 **없는 글**을 설명하는
    # 줄로 남는다 — 제목·상태·발행 주소를 전부 그 글에서 읽어 오기 때문이다
    # (2026-08-06 신고: 내 글 목록을 다 비웠는데 발행 내역은 그대로였다).
    # 네이버·스레드에 올라간 게시물은 건드리지 않는다.
    await services.scheduled_posting_service.forget_post(user.user_id, post_id)
    return Response(status_code=204)


@router.get("/posts/{post_id}/images/{index}")
async def get_post_image(request: Request, post_id: str, index: int) -> Response:
    """글의 이미지를, 실제 URL의 실제 이미지로 제공한다.

    이미지는 data URL로 저장된다 — 이미지 모델이 링크가 아니라 바이트를 반환하기
    때문이다 — 그런데 네이버는 그걸 붙여넣지 못한다: "허용되지 않는 형식의 이미지가
    있어 해당 이미지는 제외됩니다". 네이버 에디터는 가져올 수 있는 이미지만 받는다.
    그래서 복사된 HTML은 대신 여기를 가리키고, 에디터는 다른 이미지처럼 이걸 끌어온다.

    이 요청은 네이버 에디터 페이지에서 오므로 Bearer 인증은 쓸 수 없다. 대신 발행 시점에
    만든 10분 만료 HMAC capability를 검증한다. URL이 로그·클립보드에서 유출돼도 장기간
    사용자 이미지를 열 수 없고, 서명 없는 post UUID만으로는 접근할 수 없다.
    """
    from app.posting.image_url import valid_post_image_signature

    if not valid_post_image_signature(
        post_id,
        index,
        request.query_params.get("exp"),
        request.query_params.get("sig"),
    ):
        return error_response(404, "NOT_FOUND", "image not found")

    task = await _services(request).blog_task_service.get_blog_task(post_id)
    if task is None or task.final_post is None:
        return error_response(404, "NOT_FOUND", "blog task not found")

    images = task.final_post.images or []
    if not 0 <= index < len(images):
        return error_response(404, "NOT_FOUND", "image not found")

    image = images[index]
    payload = split_data_url(image.data_url)
    if payload is None:
        return error_response(404, "NOT_FOUND", "image is not a data url")

    mime, encoded = payload
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return error_response(404, "NOT_FOUND", "image could not be decoded")

    return Response(
        content=raw,
        media_type=mime,
        headers={
            "cache-control": "no-store",
            # 에디터는 우리 페이지가 아니라 자기 페이지에서 이걸 가져온다.
            "access-control-allow-origin": "*",
        },
    )


@router.put("/posts/{post_id}/input")
async def update_post_input(request: Request, post_id: str) -> JSONResponse:
    # 멱등성 키 없음: 단순 덮어쓰기라 재실행해도 무해하다.
    user = await _authorize_post(request, post_id)
    body = await _json_body(request)
    services = _services(request)
    # 소재를 고칠 때 브랜드도 바꿀 수 있다. 저장돼 있던 브랜드 자료는 걷어내고 지금 고른
    # 브랜드로 다시 채운다 — 그러지 않으면 저장할 때마다 자료가 한 벌씩 늘어난다.
    payload, limit = await with_brand_materials(
        _services(request).brand_service, user.user_id, body
    )
    task = await services.blog_task_service.update_blog_task_input(post_id, payload, limit)
    # 소재를 고쳤다 — 옛 소재로 모은 키워드는 이 글에 대해 아무것도 말해 주지 않는다.
    # 새 입력으로 다시 미리 모은다(create와 같은 이유).
    services.trend_service.start_keyword_prefetch(task)
    return envelope(task)


@router.post("/posts/{post_id}/publish")
async def publish_post(
    request: Request,
    post_id: str,
) -> JSONResponse:
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    task = await _services(request).blog_task_service.publish_blog_task(post_id, body)
    return envelope(task)


@router.post("/posts/{post_id}/search/analyze")
async def analyze_search(
    request: Request,
    post_id: str,
) -> JSONResponse:
    await _authorize_post(request, post_id)
    # 202: 이 응답 뒤에서 분석이 돌기 시작한다. GET /posts/{id} 를 폴링해서 `progress`와
    # 상태가 SEARCH_ANALYZING에서 벗어나는지 확인한다.
    task = await _services(request).blog_task_service.start_intent_analysis(post_id)
    return envelope(task, 202)


@router.post("/posts/{post_id}/search/analyze/cancel")
async def cancel_search_analysis(request: Request, post_id: str) -> JSONResponse:
    """돌고 있는 검증을 멈춘다 — 검증 화면의 '제목 다시 고르기'가 부른다.

    글의 상태는 건드리지 않는다. 제목은 이미 골라 둔 것이고, 다음 검증 요청이 그 자리에서
    새로 시작한다. 멈출 것이 없었으면(이미 끝났다) ``stopped: false``다 — 그것도 정상이라
    오류로 만들지 않는다.
    """
    await _authorize_post(request, post_id)
    stopped = await _services(request).blog_task_service.cancel_intent_analysis(post_id)
    return bare({"stopped": stopped})


@router.post("/posts/{post_id}/intents/select")
async def select_intent(request: Request, post_id: str) -> JSONResponse:
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    task = await _services(request).blog_task_service.select_intent(post_id, body)
    # 의도가 정해진 순간 콘텐츠 설계를 백그라운드에서 미리 만든다. 설계는 의도·입력·
    # 설정에만 의존하므로 지금 만들 수 있고, 사용자가 '원고 생성'을 누를 때는 설계가
    # 이미 캐시에 있어 첫 단계 대기가 사라진다. 실패해도 원고 생성이 다시 만든다.
    _services(request).draft_service.start_content_plan_prefetch(task)
    return envelope(task)


@router.post("/posts/{post_id}/draft/generate")
async def generate_draft(
    request: Request,
    post_id: str,
) -> JSONResponse:
    user = await _authorize_post(request, post_id)
    body = await _json_body(request)
    # 202, M3와 동일: 생성은 백그라운드에서 돌고 클라이언트가 폴링한다.
    #
    # owner_id를 넘기는 이유: 한 사람이 **대화형 원고 생성을 두 개** 동시에 돌리지
    # 못하게 하려는 것이다(2026-08-12). 예약 작업은 이 라우트를 지나지 않으므로 영향이 없다.
    task = await _services(request).draft_service.start_draft_generation(
        post_id, body, owner_id=user.user_id
    )
    return envelope(task, 202)


@router.put("/posts/{post_id}/draft")
async def update_draft(request: Request, post_id: str) -> JSONResponse:
    # 멱등성 키 없음: 덮어쓰기를 재실행해도 같은 덮어쓰기다.
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    task = await _services(request).draft_service.update_draft_text(post_id, body)
    return envelope(task)


@router.post("/trends/prefetch")
async def prefetch_material_keywords(request: Request) -> JSONResponse:
    """소재 입력이 끝난 낌새(글 목적·연령·참고 자료를 만지기 시작)에 소재 관련 키워드
    수집을 미리 시작한다(2026-08-10 사용자 요청). 글이 아직 없어도 된다 — 소재 풀은
    소재 단위로 저장돼 뒤에 만들어질 글이 그대로 재사용한다. fire-and-forget이라
    실패해도 화면의 요청이 어차피 다시 모은다."""
    user = await _authenticate(request)
    body = await _json_body(request)
    started = _services(request).trend_service.start_material_pool_warmup(
        user.user_id, body
    )
    return bare({"started": started})


@router.get("/trends/keywords")
async def list_stored_trend_keywords(request: Request) -> JSONResponse:
    """DB에 쌓인 트렌드 키워드를 그대로 준다 — 글이 없어도 된다.

    `/posts/{id}/trends/recommend`와 목적이 다르다. 그쪽은 **글 하나**에 맞춰 키워드를
    모으고 관련도를 채점하므로 글이 먼저 있어야 하고 소스·모델 호출이 따라온다.
    "지금 무엇이 쌓여 있나"만 보려고 빈 글을 만들 이유가 없다 — 실제로 그 빈 글이
    브랜드 자료 검증에 걸려 키워드 목록까지 함께 죽었다.
    """
    await _authenticate(request)
    try:
        limit = int(request.query_params.get("limit", 12))
    except ValueError:
        raise BlogTaskError("VALIDATION_FAILED", "limit must be an integer") from None
    shuffle = request.query_params.get("shuffle") in {"1", "true", "True"}
    keywords = await _services(request).trend_service.list_stored_keywords(limit, shuffle)
    return bare({"trendKeywords": [keyword.to_wire() for keyword in keywords]})


@router.post("/posts/{post_id}/trends/recommend")
async def recommend_trends(request: Request, post_id: str) -> JSONResponse:
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    recommendation = await _services(request).trend_service.recommend_topics(post_id, body)
    return envelope(recommendation)


@router.post("/posts/{post_id}/trends/topics")
async def generate_trend_topics(request: Request, post_id: str) -> JSONResponse:
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    result = await _services(request).trend_service.generate_topics(post_id, body)
    return envelope(result)


@router.post("/posts/{post_id}/trends/select")
async def select_trend(request: Request, post_id: str) -> JSONResponse:
    await _authorize_post(request, post_id)
    body = await _json_body(request)
    task = await _services(request).trend_service.select_topic(post_id, body)
    return envelope(task)


# --- 예약 포스팅: 기존 /posts 라우트는 그대로 두고 별도 경로로 붙인다 ---


@router.post("/posts/{post_id}/schedule", status_code=201)
async def schedule_prepared_post(request: Request, post_id: str) -> JSONResponse:
    """새 글 작성에서 방향까지 고른 글을 예약 작업으로 넘긴다(2026-08-11).

    시각은 글에 이미 저장돼 있다(``input.scheduledRunAt``) — 소재 단계에서 사용자가
    고른 값이다. 시각을 비워 둔 글은 이 경로를 지나지 않고 예전처럼 곧바로 원고를 만든다.

    본문은 **여러 편을 만들 때만** 온다(2026-08-12): ``additionalDrafts``는 뒤이어
    만들 글의 (방향, 제목) 짝이고, 고른 차례가 만들어지는 차례다. 본문이 없으면 예전
    그대로 한 편이다 — 옛 화면이 보낸 빈 요청도 그대로 통과한다.

    짝에는 **고른 방향 전체**가 함께 온다(``intent``). 편마다 검증을 다시 돌리면 글에
    남는 후보는 마지막 편의 것뿐이라, 자리번호(``intentId``)만으로는 앞 편이 무엇을
    골랐는지 되찾을 수 없기 때문이다(``validate_chosen_intent`` 참고).
    """
    user = await _authenticate(request)
    services = _services(request)
    body = await _json_body(request)
    view = await services.scheduled_posting_service.schedule_prepared_post(
        user.user_id,
        post_id,
        validate_additional_drafts(body.get("additionalDrafts")),
        # 첫 편의 짝. 여러 편을 만들 때만 온다 — 화면이 라운드마다 고른 것을 마지막에
        # 한 번에 보내기 때문이다. 없으면 글에 이미 저장된 제목·방향을 그대로 쓴다.
        primary_draft=(validate_additional_drafts([body["primaryDraft"]])[0]
                       if isinstance(body.get("primaryDraft"), dict) else None),
    )
    services.scheduled_posting_worker.wake()
    return bare(view, 201)


@router.post("/scheduled/naver/batches", status_code=201)
async def start_scheduled_batch(request: Request) -> JSONResponse:
    """예약 배치를 만들고 시작한다.

    userId는 요청 본문에서 받지 않는다 — 인증된 사용자의 것만 쓴다.
    """
    user = await _authenticate(request)
    body = await _json_body(request)
    services = _services(request)
    view = await services.scheduled_posting_service.start_batch(user.user_id, body)
    # 워커를 깨워 첫 작업을 곧바로 집어 가게 한다(기본 주기를 기다리지 않는다).
    services.scheduled_posting_worker.wake()
    return bare(view, 201)


@router.get("/scheduled/naver/batches/active")
async def active_scheduled_batch(request: Request) -> JSONResponse:
    user = await _authenticate(request)
    view = await _services(request).scheduled_posting_service.get_active_batch(user.user_id)
    # 없으면 null. 화면은 이 값으로 '예약 시작' 버튼을 되살린다.
    return bare(view)


@router.get("/scheduled/naver/batches/{batch_id}")
async def scheduled_batch(request: Request, batch_id: str) -> JSONResponse:
    user = await _authenticate(request)
    view = await _services(request).scheduled_posting_service.get_batch(user.user_id, batch_id)
    return bare(view)


@router.post("/scheduled/naver/batches/{batch_id}/pause")
async def pause_scheduled_batch(request: Request, batch_id: str) -> JSONResponse:
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.request_pause(user.user_id, batch_id)
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.post("/scheduled/naver/batches/{batch_id}/resume")
async def resume_scheduled_batch(request: Request, batch_id: str) -> JSONResponse:
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.resume(user.user_id, batch_id)
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.post("/scheduled/naver/batches/{batch_id}/stop")
async def stop_scheduled_batch(request: Request, batch_id: str) -> JSONResponse:
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.request_stop(user.user_id, batch_id)
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.post("/scheduled/naver/batches/{batch_id}/discard")
async def discard_scheduled_batch(request: Request, batch_id: str) -> JSONResponse:
    """배치를 버리고 처음으로 — 미완료 작업을 DB에서 지운다('새 예약 시작' 버튼)."""
    user = await _authenticate(request)
    services = _services(request)
    await services.scheduled_posting_service.discard(user.user_id, batch_id)
    services.scheduled_posting_worker.wake()
    # 배치가 사라졌으므로 돌려줄 뷰가 없다 — 화면은 null을 받고 입력을 초기화한다.
    return bare(None)


@router.get("/scheduled/naver/jobs")
async def list_scheduled_jobs(request: Request) -> JSONResponse:
    """예약 목록 — 배치를 넘나들며 발행 시각 순으로.

    활성 배치 조회와 다른 화면이다. 그쪽은 '지금 돌고 있는 예약 한 벌'이고, 이쪽은
    '내가 걸어 둔 예약 전부'다(끝난 배치의 것도 남는다).
    """
    user = await _authenticate(request)
    raw_limit = request.query_params.get("limit")
    limit = 50
    if raw_limit is not None:
        try:
            limit = max(1, min(200, int(raw_limit)))
        except ValueError:
            raise BlogTaskError("VALIDATION_FAILED", "limit must be an integer") from None
    items = await _services(request).scheduled_posting_service.list_scheduled_jobs(
        user.user_id, limit=limit
    )
    return bare({"items": [item.to_wire() for item in items]})


@router.patch("/scheduled/naver/jobs/{job_id}")
async def reschedule_scheduled_job(request: Request, job_id: str) -> JSONResponse:
    """예약 하나의 발행 시각·시간대·함께 발행할 플랫폼을 바꾼다."""
    user = await _authenticate(request)
    body = await _json_body(request)
    services = _services(request)
    view = await services.scheduled_posting_service.reschedule_job(user.user_id, job_id, body)
    # 시각을 앞당겼을 수 있다 — 워커가 다음 주기까지 자고 있으면 그만큼 늦게 나간다.
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.post("/scheduled/naver/jobs/{job_id}/cancel")
async def cancel_scheduled_job(request: Request, job_id: str) -> JSONResponse:
    """예약 하나를 취소한다. 문서는 남고 상태만 CANCELED가 된다(삭제와 다르다)."""
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.cancel_job(user.user_id, job_id)
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.post("/scheduled/naver/jobs/{job_id}/retry")
async def retry_scheduled_job(request: Request, job_id: str) -> JSONResponse:
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.retry_job(user.user_id, job_id)
    services.scheduled_posting_worker.wake()
    return bare(view)


@router.delete("/scheduled/naver/jobs/{job_id}")
async def delete_scheduled_job(request: Request, job_id: str) -> JSONResponse:
    """작업 하나를 큐에서 뺀다. 남은 작업은 순서대로 이어서 진행된다."""
    user = await _authenticate(request)
    services = _services(request)
    view = await services.scheduled_posting_service.delete_job(user.user_id, job_id)
    services.scheduled_posting_worker.wake()
    return bare(view)

