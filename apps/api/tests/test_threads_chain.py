"""작성창에 스레드를 여러 개 이어 붙이는 경로.

2026-08-04 사용자 결정으로 스레드 발행이 '500자 요약 하나'에서 '여러 스레드 연속 게시'로
바뀌었다. 작성창은 '스레드에 추가'를 누를 때마다 입력칸이 하나씩 늘고, 마지막에 '게시'를
한 번 누르면 전부 올라간다.

여기서 막는 것: 앞 스레드에 덧쓰는 것, 배경 피드의 입력칸을 집는 것, 중간에 실패했는데
일부만 올라간 채 성공으로 끝나는 것.
"""

import base64
from pathlib import Path

import pytest

from app.posting import threads_browser as tb
from app.posting.credentials import remember_session_account, session_account
from app.posting.threads_split import ThreadPiece

_TEST_POSTING_CREDENTIALS_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode(
    "ascii"
).rstrip("=")


@pytest.fixture(autouse=True)
def _portable_posting_credentials_key(monkeypatch):
    """Keep credential/session-marker tests portable on non-Windows CI."""
    monkeypatch.setenv("POSTING_CREDENTIALS_KEY", _TEST_POSTING_CREDENTIALS_KEY)


def piece(text: str, *images: str) -> ThreadPiece:
    return ThreadPiece(text=text, images=tuple(images))


class FakeElement:
    def __init__(
        self,
        *,
        displayed: bool = True,
        attrs: dict | None = None,
        children: dict | None = None,
        tag_name: str = "div",
    ):
        self._displayed = displayed
        self._attrs = attrs or {}
        self._children = children or {}
        self.tag_name = tag_name
        self.clicked = 0
        # 글이 실제로 들어갔는지 확인하는 코드가 읽는 값.
        self.text = ""

    def is_displayed(self) -> bool:
        return self._displayed

    def is_enabled(self) -> bool:
        return True

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def find_elements(self, _by, selector: str):
        return self._children.get(selector, [])


class FakeComposer:
    """'스레드에 추가'를 누를 때마다 입력칸이 하나 느는 작성 모달."""

    BOX_SELECTOR = "div[contenteditable='true'], [role='textbox']"
    ADD_XPATH_MARK = "스레드에 추가"

    def __init__(self, *, background_boxes: int = 1):
        # 인텐트 URL이 첫 스레드를 이미 채워 놓은 상태로 시작한다(실제와 같게).
        first = FakeElement()
        first.text = "첫째입니다."
        self.boxes = [first]
        self.add_button = FakeElement()
        # 배경 피드에도 입력칸이 있다("새로운 소식이 있나요?") — 모달 밖이다.
        self.background = [FakeElement() for _ in range(background_boxes)]

    # 모달 스코프로 동작한다.
    def find_elements(self, _by, selector: str):
        if selector == self.BOX_SELECTOR:
            return list(self.boxes)
        if self.ADD_XPATH_MARK in selector or "Add to thread" in selector:
            return [self.add_button]
        return []

    def is_displayed(self) -> bool:
        return True

    def press_add(self) -> None:
        self.boxes.append(FakeElement())


class FakeDriver:
    def __init__(self, composer: FakeComposer):
        self.composer = composer

    def find_elements(self, _by, selector: str):
        if selector == "[role='dialog']":
            return [self.composer]
        # 화면 전체 탐색 — 배경 피드의 입력칸이 여기 걸린다.
        if selector == FakeComposer.BOX_SELECTOR:
            return self.composer.background + self.composer.boxes
        return []


@pytest.fixture
def publisher():
    instance = tb.ThreadsBrowserPublisher()
    try:
        yield instance
    finally:
        instance._cleanup_image_dir()


@pytest.fixture
def wired(publisher, monkeypatch):
    """클릭·타이핑을 가로채고, '스레드에 추가' 클릭이 칸을 늘리게 잇는다."""
    typed: list[tuple[int, str]] = []
    composer = FakeComposer()
    driver = FakeDriver(composer)

    def fake_click(_driver, element):
        element.clicked += 1
        if element is composer.add_button:
            composer.press_add()

    def fake_type(_driver, box, text):
        typed.append((composer.boxes.index(box), text))
        box.text = text

    monkeypatch.setattr(tb.ThreadsBrowserPublisher, "_click", staticmethod(fake_click))
    monkeypatch.setattr(publisher, "_type_into_composer", fake_type, raising=False)
    monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(tb, "ADD_THREAD_SETTLE_SECONDS", 0)
    return publisher, driver, composer, typed


class TestAppendingThreads:
    def test_nothing_happens_for_a_single_thread(self, wired):
        """스레드가 하나면 인텐트 URL이 이미 채웠다 — 건드릴 것이 없다."""
        publisher, driver, composer, typed = wired

        publisher._append_remaining_threads(driver, [])

        assert composer.add_button.clicked == 0
        assert typed == []

    def test_each_extra_thread_gets_its_own_box(self, wired):
        publisher, driver, composer, typed = wired

        publisher._append_remaining_threads(driver, [piece("둘째입니다."), piece("셋째입니다.")])

        # '스레드에 추가'를 스레드 수만큼 누른다.
        assert composer.add_button.clicked == 2
        # 앞 스레드에 덧쓰지 않는다 — 매번 새로 생긴 마지막 칸에 넣는다.
        assert typed == [(1, "둘째입니다."), (2, "셋째입니다.")]

    def test_a_box_that_never_appears_fails_loudly(self, publisher, monkeypatch):
        """칸이 안 늘었는데 넘어가면 앞 스레드를 덮어쓴다 — 그러느니 실패가 낫다."""
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(tb, "ADD_THREAD_GROW_SECONDS", 0.3)
        monkeypatch.setattr(tb, "ADD_THREAD_SETTLE_SECONDS", 0)
        composer = FakeComposer()
        driver = FakeDriver(composer)
        # 버튼을 눌러도 칸이 늘지 않는 화면.
        monkeypatch.setattr(
            tb.ThreadsBrowserPublisher, "_click", staticmethod(lambda _d, _e: None)
        )

        with pytest.raises(RuntimeError, match="입력칸"):
            publisher._append_remaining_threads(driver, [piece("둘째입니다.")])

    def test_an_already_present_empty_box_is_used_without_clicking(
        self, publisher, monkeypatch
    ):
        """칸이 이미 있으면 누르지 않는다 — 또 누르면 빈 칸이 하나 남아 게시가 어긋난다.

        실사용 화면에서는 눌러야 칸이 는다(그것이 주 경로다). 이 가드는 화면이 바뀌어
        칸이 미리 놓이는 경우를 위한 것이다.
        """
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(tb, "ADD_THREAD_SETTLE_SECONDS", 0)
        typed: list[tuple[int, str]] = []
        composer = FakeComposer()
        composer.boxes.append(FakeElement())  # 다음 스레드가 될 빈 칸이 이미 있다
        driver = FakeDriver(composer)

        monkeypatch.setattr(
            tb.ThreadsBrowserPublisher, "_click", staticmethod(lambda _d, _e: None)
        )
        def fake_type(_driver, box, text):
            typed.append((composer.boxes.index(box), text))
            box.text = text

        monkeypatch.setattr(publisher, "_type_into_composer", fake_type, raising=False)

        publisher._append_remaining_threads(driver, [piece("둘째입니다.")])

        assert typed == [(1, "둘째입니다.")]
        assert composer.add_button.clicked == 0  # 누를 필요가 없었다


class TestBoxesAreTrackedByIdentityNotByIndex:
    """작성창에 글칸이 아닌 입력 요소가 섞여 있어도 순번이 밀리지 않아야 한다.

    실사용(2026-08-04): 6개짜리 글이 **오류 없이 2개만** 게시됐다. 순번(`boxes[index-1]`)으로
    칸을 집었는데 모달 안에 글칸 아닌 요소가 섞이면 그 순번이 통째로 밀려, 엉뚱한 요소에
    글을 넣고 조용히 넘어간다.
    """

    def _wire(self, publisher, monkeypatch, composer):
        typed: list[tuple[str, str]] = []

        def fake_click(_driver, element):
            element.clicked += 1
            if element is composer.add_button:
                composer.press_add()

        def fake_type(_driver, box, text):
            typed.append((getattr(box, "name", "?"), text))
            box.text = text

        monkeypatch.setattr(tb.ThreadsBrowserPublisher, "_click", staticmethod(fake_click))
        monkeypatch.setattr(publisher, "_type_into_composer", fake_type, raising=False)
        monkeypatch.setattr(publisher, "_attach_images", lambda *_a: None, raising=False)
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(tb, "ADD_THREAD_GROW_SECONDS", 1)
        monkeypatch.setattr(tb, "ADD_THREAD_SETTLE_SECONDS", 0)
        return typed

    def test_an_extra_input_in_the_dialog_does_not_shift_the_boxes(self, publisher, monkeypatch):
        composer = FakeComposer()
        composer.boxes[0].name = "글칸1"  # 인텐트가 채운 첫 스레드
        # 주제 고르는 칸처럼 글칸이 아닌 입력 요소가 목록 **앞**에 섞여 있다.
        stray = FakeElement()
        stray.name = "주제칸"
        original_press = composer.press_add

        def press_add():
            original_press()
            composer.boxes[-1].name = f"글칸{len(composer.boxes)}"

        composer.press_add = press_add
        composer.find_elements = lambda _by, selector: (
            [stray, *composer.boxes]
            if selector == FakeComposer.BOX_SELECTOR
            else ([composer.add_button] if FakeComposer.ADD_XPATH_MARK in selector else [])
        )
        typed = self._wire(publisher, monkeypatch, composer)

        publisher._append_remaining_threads(
            FakeDriver(composer), [piece("둘째입니다."), piece("셋째입니다.")]
        )

        # 첫 칸(인텐트가 채운 것)에 덧쓰지도, 주제칸에 넣지도 않는다.
        assert typed == [("글칸2", "둘째입니다."), ("글칸3", "셋째입니다.")]

    def test_text_that_never_lands_stops_the_publish(self, publisher, monkeypatch):
        """글이 안 들어갔는데 넘어가면 그 스레드만 조용히 사라진다 — 그러느니 멈춘다."""
        composer = FakeComposer()
        monkeypatch.setattr(tb.ThreadsBrowserPublisher, "_click", staticmethod(
            lambda _d, e: composer.press_add() if e is composer.add_button else None
        ))
        # 타이핑이 아무 효과가 없는 화면(칸을 잘못 집은 경우와 같다).
        monkeypatch.setattr(publisher, "_type_into_composer", lambda *_a: None, raising=False)
        monkeypatch.setattr(publisher, "_describe_composer", lambda *_a: None, raising=False)
        monkeypatch.setattr(tb, "ADD_THREAD_SETTLE_SECONDS", 0)

        with pytest.raises(RuntimeError, match="글이 들어가지 않았습니다"):
            publisher._append_remaining_threads(FakeDriver(composer), [piece("둘째입니다.")])


class TestAttachingImages:
    """원고에 있던 이미지를 작성창에 올린다.

    파일 선택 대화상자는 OS 창이라 Selenium이 만질 수 없다 — 숨어 있는
    `input[type='file']`에 경로를 넣으면 대화상자 없이 업로드된다.

    **못 올려도 발행을 막지 않는다.** 이 선택자는 실제 화면에서 확인한 것이 아니라,
    글이 통째로 안 올라가는 것보다 그림 없이 올라가는 편이 낫다.
    """

    PNG = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    class FileField(FakeElement):
        """경로를 받으면 미리보기가 붙는 파일 입력칸. ``uploads=False`` 면 붙지 않는다."""

        def __init__(self, composer=None, *, uploads: bool = True):
            super().__init__(tag_name="input", attrs={"type": "file"})
            self.sent: list[str] = []
            self._composer = composer
            self._uploads = uploads

        def send_keys(self, value: str) -> None:
            self.sent.append(value)
            if self._uploads and self._composer is not None:
                # 올린 파일의 미리보기는 blob: URL이다(프로필 사진은 CDN https:).
                self._composer.previews.extend(
                    FakeElement(attrs={"src": f"blob:https://www.threads.com/{order}"})
                    for order, _ in enumerate(value.splitlines())
                )

    def _driver(self, composer, field=None):
        def find(_by, selector):
            if selector == "input[type='file']":
                return [field] if field is not None else []
            if selector == "img":
                return list(composer.previews)
            return FakeComposer.find_elements(composer, _by, selector)

        composer.previews = []
        composer.find_elements = find
        return FakeDriver(composer)

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(tb, "IMAGE_UPLOAD_SETTLE_SECONDS", 0)
        monkeypatch.setattr(tb, "IMAGE_UPLOAD_TIMEOUT_SECONDS", 1.5)
        monkeypatch.setattr(tb, "IMAGE_UPLOAD_FIRST_WAIT_SECONDS", 0.8)
        monkeypatch.setattr(tb, "FILE_INPUT_SETTLE_SECONDS", 0)
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 0.4)

    def test_the_image_is_written_to_a_file_and_handed_to_the_input(self, publisher):
        composer = FakeComposer()
        field = self.FileField(composer)
        publisher._attach_images(self._driver(composer, field), 1, (self.PNG,))

        assert len(field.sent) == 1
        path = field.sent[0]
        assert path.endswith(".png")
        # data URL이 아니라 실제 파일 경로를 넣어야 한다.
        assert "data:image" not in path

    def test_several_images_go_in_one_call_separated_by_newlines(self, publisher):
        composer = FakeComposer()
        field = self.FileField(composer)
        publisher._attach_images(self._driver(composer, field), 1, (self.PNG, self.PNG))

        assert len(field.sent[0].splitlines()) == 2

    def test_it_waits_until_the_previews_actually_appear(self, publisher, caplog):
        """작성창에서는 보였는데 게시된 글에 사진이 없었다 — 업로드를 안 기다린 탓이다.

        고정 시간을 자는 대신 미리보기 개수가 실제로 늘었는지 본다.
        """
        composer = FakeComposer()
        field = self.FileField(composer)

        with caplog.at_level("INFO"):
            publisher._attach_images(self._driver(composer, field), 2, (self.PNG, self.PNG))

        assert len(composer.previews) == 2
        assert "이미지 2장을 올렸습니다" in caplog.text

    def test_a_late_avatar_does_not_count_as_an_upload(self, publisher, monkeypatch, caplog):
        """작성창이 그려지는 중에 아바타(CDN 이미지)가 뒤늦게 나타난다.

        전체 img 개수만 세면 그것을 업로드 완료로 착각해, 사진이 안 붙은 채 게시로
        넘어간다 — 사진이 가끔 빠지던 이유로 의심되는 것이다.
        """
        composer = FakeComposer()
        field = self.FileField(composer, uploads=False)
        monkeypatch.setattr(publisher, "_describe_composer", lambda *_a: None, raising=False)
        driver = self._driver(composer, field)

        # 업로드는 안 되는데, 경로를 넣은 **뒤에** 아바타가 두 장 나타난다.
        original_send = field.send_keys

        def send_keys(value: str) -> None:
            original_send(value)
            composer.previews.append(
                FakeElement(attrs={"src": "https://cdn.threads.com/avatar.jpg"})
            )
            composer.previews.append(
                FakeElement(attrs={"src": "https://cdn.threads.com/avatar2.jpg"})
            )

        field.send_keys = send_keys

        with caplog.at_level("WARNING"):
            publisher._attach_images(driver, 1, (self.PNG,))

        # 전체 개수는 늘었지만 blob: 미리보기는 하나도 안 늘었다 → 완료로 보면 안 된다.
        assert "미리보기가" in caplog.text

    def test_previews_that_never_appear_warn_instead_of_failing(self, publisher, monkeypatch, caplog):
        """그림 없이 올라가는 편이 글이 통째로 안 올라가는 것보다 낫다 — 대신 시끄럽게 남긴다."""
        composer = FakeComposer()
        field = self.FileField(composer, uploads=False)
        monkeypatch.setattr(publisher, "_describe_composer", lambda *_a: None, raising=False)

        with caplog.at_level("WARNING"):
            publisher._attach_images(self._driver(composer, field), 1, (self.PNG,))

        assert "미리보기가" in caplog.text

    def test_a_broken_image_is_skipped_not_fatal(self, publisher):
        composer = FakeComposer()
        field = self.FileField(composer)
        publisher._attach_images(
            self._driver(composer, field), 1, ("data:image/png;base64,@@@", self.PNG)
        )

        # 깨진 장은 빠지고 나머지는 올라간다.
        assert len(field.sent[0].splitlines()) == 1

    def test_a_missing_file_input_does_not_stop_publishing(self, publisher, monkeypatch):
        monkeypatch.setattr(publisher, "_describe_composer", lambda *_a: None, raising=False)

        publisher._attach_images(self._driver(FakeComposer()), 1, (self.PNG,))  # 예외 없이 지나간다

    def test_no_images_means_no_work(self, publisher):
        composer = FakeComposer()
        field = self.FileField(composer)
        publisher._attach_images(self._driver(composer, field), 1, ())

        assert field.sent == []

    def test_files_go_nowhere_when_the_modal_never_opens(self, publisher, caplog):
        """모달이 없을 때 화면 전체에서 집으면 배경 피드의 파일 입력칸이 걸린다.

        거기 넣은 사진은 미리보기도 게시물도 되지 않은 채 사라진다 — 실사용(2026-08-04)
        에서 '미리보기 30초 경고 후 게시물에 사진 없음'의 의심 원인이다. 넣지 말고 알린다.
        """
        field = self.FileField(None)

        class NoModalDriver:
            def find_elements(self, _by, selector):
                if selector == "input[type='file']":
                    return [field]
                return []

        with caplog.at_level("WARNING"):
            publisher._attach_images(NoModalDriver(), 1, (self.PNG,))

        assert field.sent == []
        assert "작성 모달이 뜨지 않았습니다" in caplog.text

    def test_a_late_modal_is_waited_for(self, publisher):
        """인텐트 URL 직후에는 모달이 아직 그려지는 중일 수 있다 — 뜰 때까지 기다린다."""
        composer = FakeComposer()
        field = self.FileField(composer)
        driver = self._driver(composer, field)
        original_find = driver.find_elements
        dialog_queries = {"count": 0}

        def find(_by, selector):
            if selector == "[role='dialog']":
                dialog_queries["count"] += 1
                if dialog_queries["count"] < 2:
                    return []  # 첫 확인 때는 모달이 아직 없다
            return original_find(_by, selector)

        driver.find_elements = find

        publisher._attach_images(driver, 1, (self.PNG,))

        assert len(field.sent) == 1
        assert len(composer.previews) == 1

    def test_a_send_that_does_not_register_is_retried_once(self, publisher, caplog):
        """미리보기는 로컬에서 즉시 그려진다 — 안 붙었다는 것은 파일이 등록되지 않았다는
        뜻이므로, 입력칸을 새로 찾아 한 번 더 넣는다(중복 위험 없음)."""
        composer = FakeComposer()
        field = self.FileField(composer, uploads=False)
        original_send = field.send_keys

        def send_keys(value: str) -> None:
            if field.sent:  # 두 번째부터는 정상 등록되는 화면
                field._uploads = True
            original_send(value)

        field.send_keys = send_keys

        with caplog.at_level("INFO"):
            publisher._attach_images(self._driver(composer, field), 1, (self.PNG, self.PNG))

        assert len(field.sent) == 2
        assert len(composer.previews) == 2
        assert "이미지 2장을 올렸습니다" in caplog.text

    def test_a_partial_attach_is_not_resent(self, publisher, monkeypatch, caplog):
        """2장 중 1장만 붙었으면 다시 넣지 않는다 — 붙은 장이 두 번 올라간다."""
        composer = FakeComposer()
        field = self.FileField(composer, uploads=False)
        original_send = field.send_keys

        def send_keys(value: str) -> None:
            original_send(value)
            composer.previews.append(
                FakeElement(attrs={"src": "blob:https://www.threads.com/only-one"})
            )

        field.send_keys = send_keys
        monkeypatch.setattr(publisher, "_describe_composer", lambda *_a: None, raising=False)

        with caplog.at_level("WARNING"):
            publisher._attach_images(self._driver(composer, field), 1, (self.PNG, self.PNG))

        assert len(field.sent) == 1
        assert "1장만 붙었습니다" in caplog.text


class TestFindingTheAddThreadButton:
    """어느 요소를 누를지 고르는 규칙.

    글자로 찾으면 그 글자를 품은 **조상까지** 걸린다. 문서 순서 그대로 첫 번째를 누르면
    가장 바깥 래퍼를 집고, 좌표 클릭이 그 래퍼의 빈 공간을 눌러 아무 일도 일어나지 않는다.
    """

    class Driver(FakeDriver):
        def __init__(self, composer, matches):
            super().__init__(composer)
            self.matches = matches

        def execute_script(self, _script, ancestor, descendant):
            # 래퍼가 버튼을 품고 있다는 사실만 흉내 낸다.
            return ancestor is self.matches[0] and descendant is not self.matches[0]

    def test_a_real_button_beats_the_wrapper_that_contains_it(self, publisher):
        wrapper = FakeElement(tag_name="div")
        button = FakeElement(tag_name="div", attrs={"role": "button"})
        composer = FakeComposer()
        composer.find_elements = lambda _by, selector: (
            [wrapper, button] if FakeComposer.ADD_XPATH_MARK in selector else []
        )

        candidates = publisher._add_thread_candidates(self.Driver(composer, [wrapper, button]))

        assert candidates[0] is button

    def test_all_candidates_are_kept_as_fallbacks(self, publisher):
        """첫 후보가 빗나갈 수 있다 — 나머지도 순서대로 눌러 볼 수 있게 남긴다."""
        wrapper = FakeElement(tag_name="div")
        button = FakeElement(tag_name="button")
        composer = FakeComposer()
        composer.find_elements = lambda _by, selector: (
            [wrapper, button] if FakeComposer.ADD_XPATH_MARK in selector else []
        )

        candidates = publisher._add_thread_candidates(self.Driver(composer, [wrapper, button]))

        assert set(candidates) == {wrapper, button}
        assert len(candidates) <= tb.ADD_THREAD_MAX_CANDIDATES


class TestJudgingThatThePostWentThrough:
    """게시됐는데 실패로 보고하지 않는다.

    실사용(2026-08-04): 프로필에는 글이 올라가 있는데 "게시 버튼을 눌렀지만 작성창이
    닫히지 않았습니다"로 실패했다. 화면 **전체**에서 '게시' 버튼을 세었는데 작성창이 닫힌
    뒤에도 뒤 피드에 그 버튼이 남아 있었던 것이다.
    """

    def test_the_background_feed_does_not_look_like_an_open_composer(self, publisher):
        composer = FakeComposer(background_boxes=3)
        driver = FakeDriver(composer)
        composer.boxes.clear()  # 작성 모달의 입력칸은 사라졌다

        assert publisher._composer_open(driver) is False

    def test_a_dialog_with_a_textbox_is_an_open_composer(self, publisher):
        assert publisher._composer_open(FakeDriver(FakeComposer())) is True

    def test_a_composer_that_stays_open_does_not_raise(self, publisher, monkeypatch):
        """여기서 실패시키지 않는다 — 최종 판정은 프로필 확인(_verify_on_profile)이 한다."""
        monkeypatch.setattr(tb, "PUBLISH_CONFIRM_TIMEOUT_SECONDS", 0.5)

        publisher._wait_for_composer_closed(FakeDriver(FakeComposer()))  # 예외 없이 지나간다


def test_the_publish_click_waits_long_enough_for_a_chain_with_images():
    """스레드 여러 개 + 사진이면 게시가 몇 초로 끝나지 않는다 — 그 전에 또 누르면 안 된다.

    4초였을 때, 작성창에는 6개가 다 들어갔는데 게시된 것은 첫 개뿐이었다(2026-08-04).
    발행 중인 것을 한 번 더 누른 것이 유력한 원인이라 대기를 늘렸다.
    """
    assert tb.POST_CLICK_CONFIRM_SECONDS >= 15


class TestClickingPost:
    """'게시'가 눌리지 않았을 때 포기하지 않는다.

    실사용(2026-08-04)에서 버튼을 찾고도 게시가 되지 않았다. 좌표 클릭은 겹친 요소에
    가로막힐 수 있고, React가 합성 클릭을 흘리기도 한다. 그래서 좌표 → 요소 순으로
    시도하고 **작성창이 닫혔는지**로 성공을 판정한다.
    """

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        # 닫힘 확인을 짧게 — 실제 값(20초)이면 실패 경로에서 테스트가 40초 멈춘다.
        monkeypatch.setattr(tb, "POST_CLICK_CONFIRM_SECONDS", 0.5)

    class Driver(FakeDriver):
        def __init__(self, composer, *, closes_on: str | None):
            super().__init__(composer)
            self.closes_on = closes_on
            self.js_calls: list[str] = []

        def execute_script(self, script, *_args):
            self.js_calls.append(script)
            if "click()" in script and self.closes_on == "js":
                self.composer.boxes.clear()

    def test_the_coordinate_click_alone_is_enough(self, publisher, monkeypatch):
        composer = FakeComposer()
        driver = self.Driver(composer, closes_on=None)
        monkeypatch.setattr(
            tb.ThreadsBrowserPublisher,
            "_click",
            staticmethod(lambda _d, _e: composer.boxes.clear()),
        )

        publisher._click_post(driver, FakeElement())

        # 첫 시도로 닫혔으면 두 번째 방법(요소 직접 클릭)을 쓰지 않는다.
        assert not any("click()" in script for script in driver.js_calls)

    def test_it_falls_back_to_clicking_the_element_itself(self, publisher, monkeypatch):
        """좌표 클릭이 먹히지 않으면 요소를 직접 누른다."""
        composer = FakeComposer()
        driver = self.Driver(composer, closes_on="js")
        monkeypatch.setattr(
            tb.ThreadsBrowserPublisher, "_click", staticmethod(lambda _d, _e: None)
        )

        publisher._click_post(driver, FakeElement())

        assert any("click()" in script for script in driver.js_calls)
        assert composer.boxes == []

    def test_a_button_that_never_works_leaves_a_diagnosis(self, publisher, monkeypatch):
        """둘 다 실패하면 화면을 기록해 둔다 — 다음 판정(_wait_for_composer_closed)이 막는다."""
        described = {"n": 0}
        monkeypatch.setattr(
            tb.ThreadsBrowserPublisher, "_click", staticmethod(lambda _d, _e: None)
        )
        monkeypatch.setattr(
            publisher,
            "_describe_composer",
            lambda *_a: described.__setitem__("n", described["n"] + 1),
            raising=False,
        )

        publisher._click_post(self.Driver(FakeComposer(), closes_on=None), FakeElement())

        assert described["n"] == 1


class TestTheWholeRun:
    """게시까지 갔는데 마지막 확인 단계에서 죽는 일이 없어야 한다.

    2026-08-04: 스레드가 '하나'에서 '여러 개'로 바뀌며 `text` → `texts`로 이름이 바뀌었는데
    프로필 확인 호출만 옛 이름을 그대로 써서 NameError로 끝났다. 실제로는 올라갔는데
    실패로 보고되는, 가장 나쁜 형태의 실패다.
    """

    def test_the_first_thread_confirms_the_post(self, publisher, monkeypatch, tmp_path):
        seen: dict = {}
        driver = FakeDriver(FakeComposer())
        driver.get = lambda url: seen.__setitem__("url", url)
        driver.quit = lambda: None

        def confirm(_driver, text):
            seen["confirm"] = text
            return "https://www.threads.net/@me/post/1"

        monkeypatch.setattr(tb, "_create_driver", lambda _config, _headless: driver)
        for name in (
            "_ensure_logged_in",
            "_append_remaining_threads",
            "_attach_images",
            "_set_topic",
            "_click_post",
            "_wait_for_composer_closed",
        ):
            monkeypatch.setattr(publisher, name, lambda *_a, **_k: None, raising=False)
        monkeypatch.setattr(
            publisher, "_wait_for_post_button", lambda *_a: FakeElement(), raising=False
        )
        monkeypatch.setattr(publisher, "_verify_on_profile", confirm, raising=False)
        publisher._headless = True  # 창을 붙잡아 두지 않는다

        url = publisher._run_sync(
            tb.ThreadsBrowserConfig(profile_dir=tmp_path), [piece("첫째입니다."), piece("둘째입니다.")]
        )

        assert url == "https://www.threads.net/@me/post/1"
        # 확인 조각은 첫 스레드에서 뽑는다 — 프로필에 먼저 보이는 글이다.
        assert seen["confirm"] == "첫째입니다."
        # 인텐트 URL도 첫 스레드로 연다.
        assert "intent/post" in seen["url"]


class TestTheTopicIsBestEffort:
    """'커뮤니티 또는 주제'는 분류 태그다 — 못 넣어도 발행을 막지 않는다."""

    def test_a_missing_field_does_not_stop_publishing(self, publisher, monkeypatch):
        monkeypatch.setattr(publisher, "_find_menu_item", lambda *_a: None, raising=False)

        publisher._set_topic(FakeDriver(FakeComposer()), "델타포스")  # 예외 없이 지나간다

    def test_an_empty_topic_is_skipped(self, publisher, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            publisher,
            "_find_menu_item",
            lambda *_a: called.__setitem__("n", called["n"] + 1),
            raising=False,
        )

        publisher._set_topic(FakeDriver(FakeComposer()), "   ")

        assert called["n"] == 0  # 찾아볼 것도 없다


class TestComposerBoxesAreScopedToTheDialog:
    def test_background_feed_boxes_are_not_counted(self, publisher):
        """배경 피드의 '새로운 소식이 있나요?' 칸을 세면 개수 판정이 어긋난다.

        이 저장소가 두 번 겪은 함정이다 — Selenium은 가려진 요소도 보인다고 본다.
        """
        composer = FakeComposer(background_boxes=3)
        driver = FakeDriver(composer)

        # 모달 안에는 칸이 하나뿐이다(배경 3개는 세지 않는다).
        assert len(publisher._composer_boxes(driver)) == 1


class TestThreadsAccountSwitch:
    """설정에서 스레드 계정을 바꾸면 그 계정으로 올라가는지.

    네이버와 같은 구조의 버그였다. Chrome 프로필은 블로그잇 사용자별로만 갈리므로,
    스레드 계정을 바꿔도 **예전 계정 세션이 그대로 살아 있어** 그 계정으로 올라갔다.
    """

    class Credentials:
        def __init__(self, username):
            self.username = username
            self.password = "pw"

    def _belongs(self, profile, credentials):
        return tb.ThreadsBrowserPublisher._session_belongs_to_settings(profile, credentials)

    def test_the_same_account_keeps_the_session(self, tmp_path: Path):
        remember_session_account(tmp_path, "boo_ra.a")

        assert self._belongs(tmp_path, self.Credentials("boo_ra.a")) is True

    def test_a_changed_account_does_not_keep_the_session(self, tmp_path: Path, caplog):
        """여기가 핵심이다. True를 돌려주면 남의 스레드 계정에 글이 올라간다."""
        remember_session_account(tmp_path, "old_account")

        assert self._belongs(tmp_path, self.Credentials("new_account")) is False
        assert "old_account" not in caplog.text
        assert "new_account" not in caplog.text

    def test_the_comparison_ignores_letter_case(self, tmp_path: Path):
        remember_session_account(tmp_path, "Boo_Ra.A")

        assert self._belongs(tmp_path, self.Credentials("boo_ra.a")) is True

    def test_an_unknown_account_logs_in_once(self, tmp_path: Path):
        assert self._belongs(tmp_path, self.Credentials("boo_ra.a")) is False

    def test_without_credentials_the_session_is_kept(self, tmp_path: Path):
        """자동 로그인할 방법이 없다. 세션을 버리면 발행 자체가 불가능해진다."""
        remember_session_account(tmp_path, "someone")

        assert self._belongs(tmp_path, None) is True

    def test_signing_out_clears_the_cookies_and_the_record(self, tmp_path: Path):
        """스레드는 네이버와 달리 쿠키를 지운다 — 2단계 인증이 코드 입력이라 자동 처리된다."""
        remember_session_account(tmp_path, "old_account")

        class Driver:
            wiped = False

            def delete_all_cookies(self):
                Driver.wiped = True

        tb.ThreadsBrowserPublisher._sign_out(Driver(), tmp_path)

        assert Driver.wiped is True
        assert session_account(tmp_path) is None

    def test_a_broken_driver_does_not_stop_the_login(self, tmp_path: Path):
        class Exploding:
            def delete_all_cookies(self):
                raise RuntimeError("창이 닫혔습니다")

        tb.ThreadsBrowserPublisher._sign_out(Exploding(), tmp_path)  # 예외가 없으면 통과다
