"""스레드(Threads) 발행 — 실을 내용 선택 규칙과 브라우저 발행 경로.

Meta 공식 API 발행기는 2026-08-10에 제거했다(사용자 결정: 발행은 네이버처럼 브라우저
자동화만 쓴다). 무엇을 몇 개로 나눠 올리는지의 규칙은 threads_split으로 옮겨져 그대로
살아 있고, 나누는 규칙 자체는 test_threads_split.py에 있다.
"""

from app.posting import PublishJob
from app.posting.threads_split import THREAD_TEXT_LIMIT, publish_pieces_for
from app.shared import FinalPost, PostingChannel, PostingMethod, PostingResultStatus

LONG_BODY = " ".join(
    f"{n}번 문장입니다. 스레드 한도를 넘길 만큼 긴 본문을 만들기 위한 채움 문장입니다."
    for n in range(1, 40)
)


def build_post(**overrides) -> FinalPost:
    defaults = dict(
        title="스레드 발행 테스트 제목",
        body="짧은 본문입니다.",
        hashtags=["AI", "블로그"],
        html_content="<article><h1>스레드 발행 테스트 제목</h1></article>",
    )
    return FinalPost(**{**defaults, **overrides})


def build_job(post: FinalPost | None = None) -> PublishJob:
    return PublishJob(
        post_id="post_1",
        user_id="user_1",
        method=PostingMethod.AUTO,
        final_post=post or build_post(),
        channel=PostingChannel.THREADS,
    )


def texts_for(job: PublishJob) -> list[str]:
    return [piece.text for piece in publish_pieces_for(job) if piece.text]


class TestWhatGoesOnThreads:
    """무엇을 올릴지 고르는 규칙. 나누는 규칙 자체는 test_threads_split.py에 있다."""

    def test_the_article_is_carried_as_is_not_summarised(self):
        """요약하지 않는다 — 원고 문장이 그대로 스레드에 실린다(2026-08-04 사용자 결정)."""
        job = build_job(build_post(body="첫 문단입니다.\n\n둘째 문단입니다."))

        joined = "\n".join(texts_for(job))

        assert "첫 문단입니다." in joined
        assert "둘째 문단입니다." in joined

    def test_a_long_article_becomes_several_threads(self):
        job = build_job(build_post(body=LONG_BODY))

        texts = texts_for(job)

        assert len(texts) > 1
        assert all(len(text) <= THREAD_TEXT_LIMIT for text in texts)

    def test_explicit_texts_take_priority(self):
        """바깥에서 글을 직접 지정하면 그것이 나간다(순서가 곧 게시 순서다)."""
        job = build_job()
        job.threads_texts = ["첫 줄이 훅이다.", "본문 문단.", "마지막 정리."]

        assert texts_for(job) == ["첫 줄이 훅이다.", "본문 문단.", "마지막 정리."]

    def test_blank_threads_are_dropped(self):
        """빈 스레드를 그대로 올리면 빈 게시물이 하나 끼어 나간다."""
        job = build_job()
        job.threads_texts = ["첫 줄이 훅이다.", "  ", "마지막 정리."]

        assert texts_for(job) == ["첫 줄이 훅이다.", "마지막 정리."]

    # 원고 그림 전부를 스레드에 나눠 싣는 규칙(2026-08-10 사용자: "스레드에는 첫 번째
    # 이미지만 들어갔어"). 표지는 첫 스레드, 나머지는 둘째 스레드부터 한 장씩이다.
    IMG1 = "data:image/png;base64,cover1"
    IMG2 = "data:image/png;base64,body2"
    IMG3 = "data:image/png;base64,body3"
    IMG4 = "data:image/png;base64,body4"

    def _post_with_images(self, *urls: str) -> FinalPost:
        markdown = "\n\n".join(
            [f"![그림 {n}]({url})" for n, url in enumerate(urls, start=1)] + ["본문."]
        )
        return build_post(markdown_content=markdown)

    def test_every_article_image_is_spread_over_the_threads(self):
        job = build_job(self._post_with_images(self.IMG1, self.IMG2, self.IMG3))
        job.threads_texts = ["훅.", "본문 하나.", "정리."]

        pieces = publish_pieces_for(job)

        assert [piece.images for piece in pieces] == [
            (self.IMG1,),
            (self.IMG2,),
            (self.IMG3,),
        ]

    def test_leftover_images_pile_on_the_last_thread(self):
        """그림이 스레드보다 많으면 남는 그림은 마지막 스레드에 몰린다 — 버리지 않는다."""
        job = build_job(
            self._post_with_images(self.IMG1, self.IMG2, self.IMG3, self.IMG4)
        )
        job.threads_texts = ["훅.", "정리."]

        pieces = publish_pieces_for(job)

        assert pieces[0].images == (self.IMG1,)
        assert pieces[1].images == (self.IMG2, self.IMG3, self.IMG4)

    def test_a_single_thread_carries_every_image(self):
        job = build_job(self._post_with_images(self.IMG1, self.IMG2))
        job.threads_texts = ["한 덩어리 글."]

        pieces = publish_pieces_for(job)

        assert pieces[0].images == (self.IMG1, self.IMG2)

    def test_threads_without_matching_images_stay_bare(self):
        """그림이 모자라면 뒤 스레드는 그림 없이 나간다 — 아무거나 채우지 않는다."""
        job = build_job(self._post_with_images(self.IMG1))
        job.threads_texts = ["훅.", "본문.", "정리."]

        pieces = publish_pieces_for(job)

        assert [piece.images for piece in pieces] == [(self.IMG1,), (), ()]


class TestThreadsBrowserPublisher:
    """브라우저 발행 경로 — 셀레니움 없이 검증 가능한 부분만.

    실제 threads.net 조작(로그인 대기·게시 버튼·프로필 확인)은 네이버와 같은 이유로
    테스트하지 않는다: 실계정이 필요하고, 실패는 어느 단계인지 이름을 대는 RuntimeError로
    시끄럽게 드러난다.
    """

    async def test_draft_method_is_refused_before_any_browser_opens(self):
        from app.posting.threads_browser import ThreadsBrowserPublisher

        job = build_job()
        job.method = PostingMethod.DRAFT

        result = await ThreadsBrowserPublisher().publish(job)

        assert result.result == PostingResultStatus.FAIL
        assert "임시저장" in result.error_message

    async def test_selenium_failure_does_not_leak_url_path_or_token(
        self, monkeypatch, caplog
    ):
        from app.posting import threads_browser as tb

        async def broken_browser_call(_operation):
            raise RuntimeError(
                "https://example.invalid/callback?access_token=LEAKED_TOKEN "
                r"C:\private\threads-profile"
            )

        monkeypatch.setattr(tb, "_in_browser_thread", broken_browser_call)

        result = await tb.ThreadsBrowserPublisher().publish(build_job())

        assert result.result == PostingResultStatus.FAIL
        assert "관리자에게 문의" in result.error_message
        combined = result.error_message + caplog.text
        assert "LEAKED_TOKEN" not in combined
        assert "example.invalid" not in combined
        assert "threads-profile" not in combined
        assert "RuntimeError" in caplog.text

    def test_the_intent_url_carries_the_text_urlencoded(self):
        from app.posting.threads_browser import _intent_url

        url = _intent_url("제목 한 줄\n\n본문 #태그")

        assert url.startswith("https://www.threads.net/intent/post?text=")
        assert "%23%ED%83%9C%EA%B7%B8" in url  # '#태그' — #이 원문으로 새면 프래그먼트가 된다
        assert " " not in url and "\n" not in url

    def test_browser_profiles_are_scoped_per_blogit_user(self):
        from app.posting.threads_browser import threads_profile_dir

        first = threads_profile_dir("user_1")
        second = threads_profile_dir("user_2")

        assert first != second
        assert first.parent.name == ".threads-profile-users"
        assert "user_1" not in str(first)

    def test_an_empty_profile_directory_is_not_a_session(self, tmp_path):
        from app.posting.threads_browser import has_threads_session

        empty = tmp_path / "profile"
        empty.mkdir()

        assert has_threads_session(empty) is False

    def test_a_chrome_cookie_database_counts_as_a_session(self, tmp_path):
        from app.posting.threads_browser import has_threads_session

        cookies = tmp_path / "profile" / "Default" / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b"sqlite")

        assert has_threads_session(tmp_path / "profile") is True

    def test_plaintext_temp_images_are_removed_after_use(self, tmp_path, monkeypatch):
        from app.posting import threads_browser as tb

        temp_root = tmp_path / "temp"
        temp_root.mkdir()
        monkeypatch.setattr(tb.tempfile, "gettempdir", lambda: str(temp_root))
        publisher = tb.ThreadsBrowserPublisher()
        image_dir = temp_root / f"{tb.THREAD_IMAGE_TEMP_PREFIX}active"
        image_dir.mkdir()
        (image_dir / "private.jpg").write_bytes(b"generated-copy")
        publisher._image_dir = image_dir

        publisher._cleanup_image_dir()

        assert not image_dir.exists()
        assert publisher._image_dir is None

    def test_stale_cleanup_only_removes_owned_old_directories(self, tmp_path, monkeypatch):
        import os

        from app.posting import threads_browser as tb

        temp_root = tmp_path / "temp"
        temp_root.mkdir()
        monkeypatch.setattr(tb.tempfile, "gettempdir", lambda: str(temp_root))
        old = temp_root / f"{tb.THREAD_IMAGE_TEMP_PREFIX}old"
        fresh = temp_root / f"{tb.THREAD_IMAGE_TEMP_PREFIX}fresh"
        unrelated = temp_root / "unrelated"
        for directory in (old, fresh, unrelated):
            directory.mkdir()
        os.utime(old, (1, 1))
        os.utime(fresh, (95, 95))
        os.utime(unrelated, (1, 1))

        removed = tb.cleanup_stale_thread_image_dirs(max_age_seconds=10, now=100)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()
        assert unrelated.exists()


class TestConnectedThreadsPublisher:
    """스레드 발행은 브라우저 경로 하나다 — 공식 API 경로는 2026-08-10에 제거됐다."""

    class _FakePublisher:
        def __init__(self, name: str, log: list):
            self._name = name
            self._log = log

        async def publish(self, job):
            self._log.append(self._name)
            return PostingResultStatus.SUCCESS

    async def test_browser_is_the_only_path(self, monkeypatch):
        from app.posting.publisher import ConnectedThreadsPublisher

        used: list = []
        monkeypatch.delenv("THREADS_PUBLISH_MODE", raising=False)
        monkeypatch.setattr(
            "app.posting.threads_browser.ThreadsBrowserPublisher",
            lambda: self._FakePublisher("browser", used),
        )

        await ConnectedThreadsPublisher().publish(build_job())

        assert used == ["browser"]

    async def test_a_leftover_api_mode_variable_is_ignored(self, monkeypatch):
        """옛 .env에 THREADS_PUBLISH_MODE=api가 남아 있어도 브라우저로 발행한다 —
        지워진 API 경로로 빠져 조용히 실패하는 일이 없어야 한다."""
        from app.posting.publisher import ConnectedThreadsPublisher

        used: list = []
        monkeypatch.setenv("THREADS_PUBLISH_MODE", "api")
        monkeypatch.setattr(
            "app.posting.threads_browser.ThreadsBrowserPublisher",
            lambda: self._FakePublisher("browser", used),
        )

        await ConnectedThreadsPublisher().publish(build_job())

        assert used == ["browser"]
