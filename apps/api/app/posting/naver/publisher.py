"""네이버 발행 오케스트레이션과 진입점.

로그인 → 에디터 진입 → 붙여넣기 → 발행을 하나로 엮는다. 발행이 끝나면 창을 닫지 않고
발행된 글 주소로 옮겨 사용자가 결과를 볼 수 있게 남긴다. 세션 저장·붙여넣기 미리보기용
진입점도 여기 둔다.
"""

from __future__ import annotations

import logging
import time

from app.shared import PostingMethod, PostingResultStatus
from app.shared.ids import short

from ..publisher import PublishJob, PublishResult
from ..browser_reaper import mark_kept_open
from ..config import remember_blog_address
from ..live_view import hub as live_view_hub
from .browser import (
    NaverConfig,
    ProfileBusy,
    close_browser,
    use_profile,
    _create_driver,
    _has_live_session,
    _in_browser_thread,
    _NeedsHuman,
)
from .constants import SETTINGS_LOGIN_TIMEOUT_SECONDS
from .editor import SmartEditorOne, paste_mode
from .login import NaverLogin
from .plan import NaverPlanError, NaverPublishPlan, build_naver_publish_plan

logger = logging.getLogger(__name__)


# 발행 뒤 사용자가 결과를 볼 수 있게 열어 둔 브라우저를 프로필별로 하나만 붙잡아 둔다.
# 다음 발행이 시작될 때 이전 창을 닫아 창 누적과 프로필 잠금을 막는다.
_KEPT_OPEN_BROWSERS: dict = {}


def _release_kept_open_browser(profile_dir) -> None:
    driver = _KEPT_OPEN_BROWSERS.pop(str(profile_dir), None)
    if driver is not None:
        # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
        close_browser(driver)


class NaverBrowserPublisher:
    """기존 BlogPublisher 계약을 구현하는 Selenium 임시저장·발행기."""

    def __init__(self, config: NaverConfig, headless: bool = False):
        self._config = config
        self._headless = headless

    async def publish(self, job: PublishJob) -> PublishResult:
        if job.method not in (PostingMethod.DRAFT, PostingMethod.AUTO):
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="NaverBrowserPublisher only supports draft or auto publishing",
            )
        if not self._config.has_session and not self._config.can_log_in:
            return PublishResult(
                result=PostingResultStatus.NEEDS_HUMAN,
                error_message=(
                    "네이버 로그인 정보가 없습니다. 설정에서 암호화된 네이버 로그인 정보를 "
                    "저장하거나 열린 Chrome에서 직접 로그인해 주세요."
                ),
            )

        # 본문 전체를 앵커 토큰이 든 스캐폴드 HTML 하나로 만든다. 이미지는 실제 바이트로
        # 앵커 교체 시 넣어 네이버가 자기 서버에 업로드하게 한다(localhost URL은 발행 후
        # 깨진다). 해시태그는 본문이 아니라 네이버 '태그'로 등록한다(_enter_tags).
        action = "임시저장" if job.method == PostingMethod.DRAFT else "자동 발행"
        try:
            plan = build_naver_publish_plan(job.final_post, job.post_id)
        except NaverPlanError:
            # 계획 오류에도 원고의 원본 URL/data URL 조각이 들어갈 수 있다.
            logger.warning(
                "네이버 발행 계획 생성 실패 | %s - NaverPlanError",
                short(job.post_id),
            )
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message=(
                    "발행용 원고를 준비하지 못했습니다. 원고 형식을 확인한 뒤 "
                    "다시 시도해 주세요."
                ),
            )
        logger.info("네이버 Selenium %s 시작 | %s", action, short(job.post_id))
        try:
            post_url = await self._run(plan, job.final_post.hashtags, job.method)
        except ProfileBusy as error:
            # **추가 인증이 아니다.** 사람이 인증을 다시 해도 풀리지 않는다 — 앞 작업이
            # Chrome을 놓지 못한 것이므로 실패로 알리고 다시 시도하게 둔다.
            logger.warning("네이버 %s 대기 초과 | %s - %s", action, short(job.post_id), error)
            return PublishResult(
                result=PostingResultStatus.FAIL, error_message=str(error)
            )
        except _NeedsHuman as error:
            return PublishResult(
                result=PostingResultStatus.NEEDS_HUMAN, error_message=str(error)
            )
        except Exception as error:
            # Selenium 예외 문자열/traceback에는 현재 URL, 로컬 프로필 경로, query token이
            # 섞일 수 있다. 운영 로그에는 오류 타입만 남기고 화면에는 고정 문구만 보낸다.
            logger.warning(
                "네이버 Selenium %s 실패 | %s - %s",
                action,
                short(job.post_id),
                type(error).__name__,
            )
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message=(
                    f"네이버 {action}에 실패했습니다. 잠시 후 다시 시도하거나 "
                    "관리자에게 문의해 주세요."
                ),
            )

        logger.info("네이버 Selenium %s 완료 | %s → %s", action, short(job.post_id), post_url)
        return PublishResult(result=PostingResultStatus.SUCCESS, post_url=post_url)

    async def _run(
        self,
        plan: NaverPublishPlan,
        tags: list[str] | None = None,
        method: PostingMethod = PostingMethod.AUTO,
    ) -> str | None:
        return await _in_browser_thread(lambda: self._run_sync(plan, tags, method))

    def _run_sync(
        self,
        plan: NaverPublishPlan,
        tags: list[str] | None = None,
        method: PostingMethod = PostingMethod.AUTO,
    ) -> str | None:
        # **차례를 기다린다.** 프로필 하나에 Chrome 하나다(browser.use_profile).
        # 예약 두 건의 원고가 나란히 완성되면 발행도 나란히 시작되는데, 그때 뒤 건이
        # 앞 건의 Chrome에 부딪혀 죽던 것을 여기서 막는다(2026-08-07).
        with use_profile(self._config.profile_dir):
            return self._run_locked(plan, tags, method)

    def _run_locked(
        self,
        plan: NaverPublishPlan,
        tags: list[str] | None = None,
        method: PostingMethod = PostingMethod.AUTO,
    ) -> str | None:
        # 직전 발행에서 확인용으로 열어 둔 창이 있으면 닫는다(프로필 잠금·창 누적 방지).
        _release_kept_open_browser(self._config.profile_dir)
        driver = _create_driver(self._config, self._headless)
        # 이 크롬 화면을 웹으로 중계한다 — 외부 PC 사용자가 로그인·인증 화면을 보고
        # 직접 조작할 수 있다. 등록 실패는 발행을 막지 않는다.
        if self._config.user_id:
            live_view_hub.register(
                self._config.user_id,
                "naver",
                driver,
                "네이버 임시저장" if method == PostingMethod.DRAFT else "네이버 발행",
                kind="publish",
            )
        try:
            logger.info(
                "[NAVER_PUBLISH] mode=%s user=%s method=%s — 발행 흐름 시작",
                paste_mode(),
                short(self._config.user_id) if self._config.user_id else "-",
                method.value,
            )
            NaverLogin(driver, self._config).ensure_logged_in()
            editor = SmartEditorOne(driver)
            # 실제로 열린 블로그 주소를 기억해 둔다. 네이버 아이디와 블로그 주소는 다를 수
            # 있고(아이디 win-z / 주소 aiona_it), 아이디로 만든 주소는 안내창으로 막힌다.
            # 한 번 알아 두면 다음 발행은 첫 번째 경로에서 바로 들어간다.
            opened_blog_id = editor.navigate(self._config.blog_id)
            if opened_blog_id and opened_blog_id.strip().lower() != (
                self._config.blog_id or ""
            ).strip().lower():
                logger.info(
                    "블로그 주소가 로그인 식별자와 다릅니다. 다음 발행부터 확인된 주소를 씁니다."
                )
                remember_blog_address(self._config.profile_dir, opened_blog_id)
            editor.fill_publish_plan(plan)
            # 검증이 실패하면 예외가 나 저장·발행 어느 쪽도 눌리지 않고, 브라우저는
            # 아래 finally 정책대로 열려 남아 사용자가 결과를 직접 확인할 수 있다.
            editor.validate_publish_plan(plan)
            if method == PostingMethod.DRAFT:
                # 임시저장도 발행과 똑같이 태그를 넣는다 — 마지막에 누르는 버튼만 다르다.
                if not editor.save_draft(tags):
                    raise RuntimeError(
                        "임시저장 버튼(발행 옆 '저장')을 누르지 못했습니다. "
                        "열어 둔 네이버 창에서 직접 저장해 주세요."
                    )
                post_url = None
            else:
                post_url = editor.publish(tags)

            # 즉시 발행은 발행된 글로 옮겨 결과를 바로 보여준다. 임시저장은 작성 화면을
            # 그대로 두어 사용자가 이어서 검토할 수 있게 한다.
            if not self._headless and post_url:
                try:
                    driver.get(post_url)
                except Exception:
                    pass
            return post_url
        finally:
            # 창은 성공·실패와 관계없이 열어 둔다. 예전에는 성공 경로에서만 붙잡아 둬서,
            # 임시저장 버튼을 못 찾는 등으로 실패하면 창이 그대로 닫혔다 — 사용자는 무엇이
            # 잘못됐는지 볼 수도, 직접 저장을 마칠 수도 없었다. 실패했을 때야말로 화면이
            # 남아 있어야 한다.
            if self._headless:
                # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
                close_browser(driver)
            else:
                # 발행 흐름이 끝났다 — 창은 확인용으로 남겨도 중계 목록에서는 내린다.
                live_view_hub.mark_idle(driver)
                # 다음 발행이 닫거나, 일정 시간이 지나면 리퍼가 닫는다(browser_reaper).
                mark_kept_open(_KEPT_OPEN_BROWSERS, str(self._config.profile_dir), driver)


async def log_in_and_store_session(config: NaverConfig, headless: bool = False) -> None:
    """Selenium Chrome 프로필에 네이버 세션을 저장한다."""

    def connect() -> None:
        # 발행과 같은 프로필이다 — 발행이 돌고 있으면 그것이 끝난 뒤에 연다.
        with use_profile(config.profile_dir):
            _connect_locked()

    def _connect_locked() -> None:
        # 이전에 열어 둔 창이 있으면 닫는다 — 프로필은 한 번에 한 창만 쓸 수 있다.
        _release_kept_open_browser(config.profile_dir)
        driver = _create_driver(config, headless)
        # 설정 화면의 로그인이야말로 화면 중계가 필요한 자리다 — 외부 PC 사용자는 이
        # 중계 없이는 서버에 뜬 로그인 창(캡차·2단계 인증)을 볼 방법이 없다.
        if config.user_id:
            live_view_hub.register(config.user_id, "naver", driver, "네이버 로그인", kind="login")
        try:
            NaverLogin(
                driver, config, human_wait_seconds=SETTINGS_LOGIN_TIMEOUT_SECONDS
            ).ensure_logged_in()
            if not _has_live_session(driver):
                raise _NeedsHuman("네이버 로그인 세션 쿠키를 확인하지 못했습니다.")

            # 로그인에서 멈추지 않고 **글쓰기 화면까지 열어 둔다.** 사용자는 "발행할 준비가
            # 됐다"를 눈으로 확인하고 싶어 하고, 여기서 실제 블로그 주소도 함께 배워
            # 두면 첫 발행이 폴백 경로를 거치지 않는다.
            #
            # 여기서 실패해도 로그인 자체는 성공이다(세션은 저장됐다). 창은 열린 채
            # 남으므로 사용자가 직접 글쓰기로 들어갈 수 있다.
            try:
                opened_blog_id = SmartEditorOne(driver).navigate(config.blog_id)
            except Exception as error:
                logger.warning(
                    "로그인은 됐지만 글쓰기 화면을 열지 못했습니다 (%s)",
                    type(error).__name__,
                )
                return
            if opened_blog_id and opened_blog_id.strip().lower() != (
                config.blog_id or ""
            ).strip().lower():
                logger.info(
                    "블로그 주소가 로그인 식별자와 다릅니다. 다음 발행부터 확인된 주소를 씁니다."
                )
                remember_blog_address(config.profile_dir, opened_blog_id)
        finally:
            # 성공해도 **창을 닫지 않는다.** 사용자가 어느 계정으로 들어갔는지 눈으로
            # 확인해야 하고, 2단계 인증이 남았으면 그 창에서 마저 끝내야 한다. 예전에는
            # 로그인이 끝나자마자 닫혀서 무엇이 됐는지 볼 수가 없었다. 다음 발행이
            # 시작될 때 이 창을 닫고 새로 연다(발행 경로와 같은 정책).
            if headless:
                # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
                close_browser(driver)
            else:
                # 로그인 흐름이 끝났다 — 창은 남겨도 중계 목록에서는 내린다.
                live_view_hub.mark_idle(driver)
                mark_kept_open(_KEPT_OPEN_BROWSERS, str(config.profile_dir), driver)

    await _in_browser_thread(connect)


async def fill_editor_for_preview(
    config: NaverConfig, plan: NaverPublishPlan, hold_seconds: float = 180
) -> None:
    """저장된 세션으로 에디터를 열어 발행 계획을 붙여넣되 발행하지 않는다.

    실제 발행 경로(``NaverBrowserPublisher``)와 **같은** ``navigate`` →
    ``fill_publish_plan`` → ``validate_publish_plan``을 태우고 저장·발행만 건너뛴다.
    예전에는 미리보기가 별도의 한 번 붙여넣기(fill) 경로를 써서, 미리보기 정상이
    실제 발행 정상을 보장하지 못했다. 결과를 사람이 눈으로 확인할 수 있도록
    브라우저를 ``hold_seconds`` 동안 열어 둔 뒤 닫는다.
    """

    def run() -> None:
        # 발행과 같은 프로필이다. 미리보기는 화면을 3분 열어 두므로, 자물쇠 없이 두면
        # 그 3분 동안 시작된 예약 발행이 통째로 죽는다.
        with use_profile(config.profile_dir):
            _run_locked()

    def _run_locked() -> None:
        _release_kept_open_browser(config.profile_dir)
        driver = _create_driver(config)
        if config.user_id:
            live_view_hub.register(
                config.user_id, "naver", driver, "네이버 미리보기", kind="preview"
            )
        try:
            NaverLogin(driver, config).ensure_logged_in()
            editor = SmartEditorOne(driver)
            editor.navigate(config.blog_id)
            try:
                editor.fill_publish_plan(plan)
                editor.validate_publish_plan(plan)
            except Exception:
                # 실패한 화면이야말로 봐야 한다 — 열어 둔 채 기다렸다가 실패를 알린다.
                logger.warning(
                    "붙여넣기/검증 실패 — 화면을 %d초 동안 남겨 둡니다.", int(hold_seconds)
                )
                time.sleep(hold_seconds)
                raise
            logger.info(
                "붙여넣기·검증 완료 — 발행하지 않고 %d초 동안 열어 둡니다. (확인 후 창을 닫아도 됩니다)",
                int(hold_seconds),
            )
            time.sleep(hold_seconds)
        finally:
            # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
            close_browser(driver)

    await _in_browser_thread(run)
