"""발행 워커.

자동 발행은 구현되어 있지 않고, 정직하게 그렇다고 말하는 게 맞다. 예전에는 지어낸
blog.example.com URL과 함께 SUCCESS를 돌려줬고, 그래서 이 기능을 켠 사용자는
"발행 완료"와 아무 데도 없는 링크를 봤다 — 글은 아무 데도 올라가지 않았다. 삭제된
가짜 LLM provider와 같은 부류의 거짓말이다.

그렇다고 간단히 만들 수 있는 것도 아니다. 네이버는 2020년에 블로그 글쓰기 API를,
티스토리는 2024년에 Open API를 종료해서, 발행하려면 사용자 본인의 세션으로 브라우저에서
실제 에디터를 직접 조작해야 한다. 그게 생기기 전까지 자동 발행을 요청하면 글은
POSTING_NEEDS_HUMAN에 남는다 — 바로 그 상태가 존재하는 이유다.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.shared import FinalPost, PostingChannel, PostingMethod, PostingResultStatus


@dataclass
class PublishJob:
    post_id: str
    user_id: str
    method: PostingMethod
    final_post: FinalPost
    # 발행 목적지. 채널 개념이 없던 호출은 전부 네이버였으므로 기본값이 과거와 같다.
    channel: PostingChannel = field(default=PostingChannel.NAVER)
    # 스레드 전용으로 새로 쓴 게시물들. **순서가 곧 게시 순서**다(첫 스레드 → 답글로 이어짐).
    # 없으면 발행기가 블로그 원고 요약(500자) 하나로 폴백한다 — 스레드 원고 생성기가
    # 연결되지 않은 구성에서도 발행은 동작해야 한다.
    threads_texts: list[str] | None = None
    # 스레드 작성창의 "커뮤니티 또는 주제"에 넣을 값. 고정 분류가 아니라 주제 태그라
    # 사용자가 고른 검색어(없으면 소재)를 쓴다. 없으면 주제 없이 올린다.
    threads_topic: str | None = None


@dataclass
class PublishResult:
    result: PostingResultStatus
    post_url: str | None = None
    error_message: str | None = None


class BlogPublisher(Protocol):
    async def publish(self, job: PublishJob) -> PublishResult: ...


class CopyPublisher:
    async def publish(self, job: PublishJob) -> PublishResult:
        if job.method != PostingMethod.COPY:
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="CopyPublisher only supports copy publishing",
            )
        return PublishResult(result=PostingResultStatus.SUCCESS)


class UnimplementedAutoPublisher:
    """사실을 그대로 말한다: 아무도 아무것도 발행하지 않았다."""

    async def publish(self, job: PublishJob) -> PublishResult:
        if job.method != PostingMethod.AUTO:
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="UnimplementedAutoPublisher only supports auto publishing",
            )
        return PublishResult(
            result=PostingResultStatus.NEEDS_HUMAN,
            error_message=(
                "자동 발행은 아직 준비 중입니다. 네이버·티스토리가 외부 발행 API를 종료해"
                " 브라우저로 직접 올리는 방식이 필요합니다. 지금은 복사해서 붙여넣어 주세요."
            ),
        )


class ConnectedNaverPublisher:
    """발행 시점의 사용자별 로컬 네이버 저장 정보를 실제 네이버 작업기로 넘긴다.

    서버 시작 뒤 설정 화면에서 계정을 연결해도 즉시 반영되어야 한다. 시작 시점에
    한 번만 config를 읽으면 그 뒤 연결된 계정이 임시저장·자동 발행에 반영되지 않는다.
    """

    async def publish(self, job: PublishJob) -> PublishResult:
        if job.method not in (PostingMethod.DRAFT, PostingMethod.AUTO):
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="ConnectedNaverPublisher only supports Naver draft or auto publishing",
            )

        from .config import naver_config_from_env, naver_profile_dir
        from .credentials import load_credentials
        from .naver import NaverBrowserPublisher

        profile_dir = naver_profile_dir(job.user_id)
        remembered = load_credentials(profile_dir)
        config = (
            naver_config_from_env(
                username=remembered.username,
                password=remembered.password,
                user_id=job.user_id,
            )
            if remembered
            else naver_config_from_env(user_id=job.user_id)
        )
        if config is None:
            return PublishResult(
                result=PostingResultStatus.NEEDS_HUMAN,
                error_message="저장된 네이버 로그인 정보가 없습니다. 설정에서 네이버 계정을 저장해 주세요.",
            )
        return await NaverBrowserPublisher(config).publish(job)


def _default_auto_publisher() -> "BlogPublisher":
    """항상 발행 버튼을 누른 사용자의 네이버 저장 정보를 사용한다."""
    return ConnectedNaverPublisher()


class ConnectedThreadsPublisher:
    """사용자별 threads.net 브라우저 세션으로 발행한다 — 네이버와 같은 구조다.

    예전에는 ``THREADS_PUBLISH_MODE=api``로 Meta 공식 API 경로를 고를 수 있었지만
    2026-08-10에 걷어냈다(사용자 결정). 실사용 경로는 처음부터 브라우저였고(Meta 앱
    검수 전에는 Threads Tester 계정만 토큰을 만들 수 있다), API 경로는 연속 스레드와
    이미지 첨부도 지원하지 못했다. 그 환경변수는 이제 무시된다.

    발행마다 새로 만드는 이유는 네이버와 같다 — 설정 화면에서 방금 연결한 세션이
    다음 발행에 바로 반영되어야 한다.
    """

    async def publish(self, job: PublishJob) -> PublishResult:
        # threads_browser가 이 모듈의 PublishJob을 참조하므로 순환을 피해 늦게 불러온다.
        from .threads_browser import ThreadsBrowserPublisher

        return await ThreadsBrowserPublisher().publish(job)


def _default_threads_publisher() -> "BlogPublisher":
    return ConnectedThreadsPublisher()


class DefaultPostingWorker:
    def __init__(
        self,
        copy_publisher: BlogPublisher | None = None,
        auto_publisher: BlogPublisher | None = None,
        threads_publisher: BlogPublisher | None = None,
    ):
        self._copy_publisher = copy_publisher or CopyPublisher()
        self._auto_publisher = auto_publisher or _default_auto_publisher()
        self._threads_publisher = threads_publisher or _default_threads_publisher()

    async def publish(self, job: PublishJob) -> PublishResult:
        # 복사는 채널이 없다(클립보드가 목적지다). 그 외에는 채널이 발행기를 고른다.
        if job.method == PostingMethod.COPY:
            return await self._copy_publisher.publish(job)
        if job.channel == PostingChannel.THREADS:
            return await self._threads_publisher.publish(job)
        return await self._auto_publisher.publish(job)
