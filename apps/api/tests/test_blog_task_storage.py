"""글 문서를 어떻게 저장하고 되읽는지.

실측에서 출발한 테스트다. `blogTask` 컬렉션이 371MB / 119건(한 건 평균 3.12MB)까지
커졌고, 글 목록 하나를 그리는 데 **30KB를 얻으려고 206MB를 읽고 있었다**(450ms).

그 절반이 중복이었다. ``DraftGenerationResult.final_post``는 문서 맨 위의 ``finalPost``와
항상 같은 값인데, 안에 base64 이미지가 들어 있어 두 벌을 쓰면 문서가 정확히 두 배가 된다.
실제 글 하나를 뜯어보니 1.11MB 중 0.54MB씩 똑같은 것이 두 벌이었다.
"""

import base64

from bson import Binary
import pytest

from app.errors import BlogTaskError
from app.shared.status import BlogTaskStatus
from app.modules.blog_task.repository import (
    IMAGE_ELSEWHERE,
    WITHOUT_UNUSED_FIELDS,
    MongoBlogTaskRepository,
    _split_images,
    _to_task,
    _with_restored_final_post,
    _without_duplicate_final_post,
)


def draft_result_wire(images: list[dict] | None = None) -> dict:
    final_post = {
        "title": "여름 휴가 준비물",
        "body": "본문",
        "hashtags": ["여행"],
        "htmlContent": "<p>본문</p>",
        "images": images if images is not None else [],
    }
    return {
        "promptVersion": "v1",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "generatedAt": "2026-08-06T00:00:00.000Z",
        "finalPost": final_post,
    }


def stored_document(draft_result: dict) -> dict:
    """저장소가 실제로 쓰는 모양."""
    return {
        "postId": "post_1",
        "userId": "user_1",
        "status": "READY_TO_PUBLISH",
        "version": 1,
        "createdAt": "2026-08-06T00:00:00.000Z",
        "updatedAt": "2026-08-06T00:00:00.000Z",
        "statusHistory": [],
        "postingLogs": [],
        "input": {"topic": "여름 휴가", "keywords": [], "referenceMaterials": []},
        "draftGenerationResult": _without_duplicate_final_post(draft_result),
        "finalPost": draft_result["finalPost"],
    }


class TestNotStoringTheSameThingTwice:
    def test_the_draft_result_is_stored_without_its_copy_of_the_post(self):
        trimmed = _without_duplicate_final_post(draft_result_wire())

        assert "finalPost" not in trimmed
        # 나머지는 그대로여야 한다 — 어떤 모델이 언제 썼는지는 추적에 쓰인다.
        assert trimmed["model"] == "claude-opus-5"

    def test_the_original_is_not_mutated(self):
        """호출한 쪽이 그 값을 계속 쓴다. 지워 버리면 저장 뒤 화면 응답이 비어 버린다."""
        original = draft_result_wire()

        _without_duplicate_final_post(original)

        assert "finalPost" in original

    def test_a_document_with_images_is_half_the_size(self):
        image = {
            "dataUrl": "data:image/png;base64," + "A" * 100_000,
            "altText": "사진",
            "prompt": "p",
            "provider": "x",
            "model": "y",
            "generatedAt": "2026-08-06T00:00:00.000Z",
            "mimeType": "image/png",
        }
        both = draft_result_wire([image])
        one = _without_duplicate_final_post(both)

        # 두 벌일 때는 이미지가 두 번 들어간다.
        assert str(both).count("data:image/png") == 1  # 이 dict 안에는 한 번
        assert len(str(one)) < len(str(both)) / 2


class TestReadingItBack:
    def test_the_post_is_put_back_where_the_model_expects_it(self):
        document = stored_document(draft_result_wire())

        task = _to_task(document)

        assert task.draft_generation_result is not None
        assert task.draft_generation_result.final_post.title == "여름 휴가 준비물"
        assert task.final_post is not None
        assert task.final_post.title == "여름 휴가 준비물"

    def test_an_old_document_with_both_copies_still_reads(self):
        """이관 전 문서에는 두 벌 다 들어 있다. 그때는 손대지 않는다."""
        both = draft_result_wire()
        document = {**stored_document(both), "draftGenerationResult": both}

        task = _to_task(document)

        assert task.draft_generation_result.final_post.title == "여름 휴가 준비물"

    def test_a_document_without_a_draft_result_is_left_alone(self):
        document = stored_document(draft_result_wire())
        document.pop("draftGenerationResult")
        document.pop("finalPost")

        task = _to_task(document)

        assert task.draft_generation_result is None
        assert task.final_post is None


class TestWhenItCannotBeRestored:
    """되돌릴 수 없으면 **어느 글인지 말하고 멈춘다.**

    그대로 두면 모델 검증이 "finalPost 필드가 없다"는 말만 남기고, 어느 글에서 났는지
    알려 주지 않는다. 119건 중 한 건이 그런 상태여도 찾을 방법이 없다.
    """

    def test_the_error_names_the_post(self):
        document = stored_document(draft_result_wire())
        document.pop("finalPost")

        with pytest.raises(BlogTaskError) as raised:
            _with_restored_final_post(document)

        assert "post_1" in str(raised.value)
        assert "finalPost" in str(raised.value)

    def test_it_does_not_pretend_the_post_is_missing(self):
        """조용히 None으로 두면 '원고 없는 글'로 보여 사용자가 다시 만들게 된다."""
        document = stored_document(draft_result_wire())
        document.pop("finalPost")

        with pytest.raises(BlogTaskError):
            _to_task(document)


def image_wire(data_url: str) -> dict:
    return {
        "dataUrl": data_url,
        "altText": "사진",
        "prompt": "p",
        "provider": "x",
        "model": "y",
        "generatedAt": "2026-08-06T00:00:00.000Z",
        "mimeType": "image/png",
    }


class TestTakingImagesOutOfThePost:
    """이미지를 옆 컬렉션으로 뺀다.

    최적화가 아니라 **오류 수정**이다. 이미지가 글 문서 안에 있을 때 한 건이 1.6MB까지
    커졌고, 그 글을 여는 것이 20초 타임아웃으로 실패했다(2026-08-06 실측: 가벼운 필드만
    읽으면 20ms, 이미지까지 읽으면 실패).
    """

    def test_the_bytes_come_out_and_a_marker_stays_behind(self):
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": "<p>본문</p>",
            "images": [image_wire("data:image/png;base64,AAAA")],
        }

        light, rows = _split_images(final_post, "post_1")

        # 바이트는 base64 글자가 아니라 이진으로 담는다 — base64는 원본보다 33% 크다.
        assert rows == [
            {"postId": "post_1", "index": 0, "bytes": Binary(base64.b64decode("AAAA")), "mimeType": "image/png"}
        ]
        assert light["images"][0]["dataUrl"] == IMAGE_ELSEWHERE
        # 설명은 글 문서에 남는다 — 목록·미리보기가 이미지 없이도 쓴다.
        assert light["images"][0]["altText"] == "사진"

    def test_the_marker_is_not_an_empty_string(self):
        """빈 문자열이면 '이미지가 없다'와 구분되지 않는다.

        구분이 안 되면 못 붙인 채로 발행돼 **이미지 없는 글이 조용히 올라간다.**
        """
        assert IMAGE_ELSEWHERE
        assert IMAGE_ELSEWHERE != ""

    def test_the_featured_image_gets_its_own_slot(self):
        """대표 이미지는 -1번이다. 본문 이미지 번호와 겹치면 서로 덮어쓴다."""
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": "<p>본문</p>",
            "images": [image_wire("data:image/png;base64,BBBB")],
            "featuredImage": image_wire("data:image/png;base64,CCCC"),
        }

        light, rows = _split_images(final_post, "post_1")

        assert {row["index"] for row in rows} == {0, -1}
        assert light["featuredImage"]["dataUrl"] == IMAGE_ELSEWHERE

    def test_splitting_twice_does_not_lose_the_bytes(self):
        """이관을 두 번 돌릴 수 있다. 두 번째에는 이미 나간 것을 다시 내보내지 않는다."""
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": "<p>본문</p>",
            "images": [image_wire("data:image/png;base64,AAAA")],
        }

        light, _ = _split_images(final_post, "post_1")
        again, rows = _split_images(light, "post_1")

        assert rows == []
        assert again["images"][0]["dataUrl"] == IMAGE_ELSEWHERE

    def test_the_same_image_inside_the_text_comes_out_too(self):
        """같은 이미지가 본문 글 안에도 인라인 base64로 한 벌씩 더 들어 있었다.

        실측(2026-08-06): 글 하나에서 html 1.4MB + markdown 1.4MB, 51건이 그랬다.
        `images[]`만 빼서는 글 문서가 여전히 2~3MB라 여는 것이 20초 제한을 넘겼다.
        """
        data_url = "data:image/png;base64,AAAA"
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": f'<p>앞</p><img src="{data_url}"><p>뒤</p>',
            "markdownContent": f"앞\n\n![사진]({data_url})\n\n뒤",
            "images": [image_wire(data_url)],
        }

        light, rows = _split_images(final_post, "post_1")

        assert rows[0]["postId"] == "post_1" and rows[0]["index"] == 0
        assert bytes(rows[0]["bytes"]) == base64.b64decode("AAAA")
        # 바이트는 어디에도 남지 않는다.
        assert "base64" not in light["htmlContent"]
        assert "base64" not in light["markdownContent"]
        # 몇 번째 이미지인지는 남는다.
        assert "stored:post_images#0" in light["htmlContent"]
        assert "stored:post_images#0" in light["markdownContent"]
        # 나머지 글은 그대로다.
        assert "<p>앞</p>" in light["htmlContent"]

    def test_the_featured_image_in_the_text_uses_its_own_number(self):
        data_url = "data:image/png;base64,CCCC"
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": f'<img src="{data_url}">',
            "images": [],
            "featuredImage": image_wire(data_url),
        }

        light, _ = _split_images(final_post, "post_1")

        assert "stored:post_images#-1" in light["htmlContent"]

    def test_a_post_without_images_is_left_alone(self):
        final_post = {"title": "제목", "body": "본문", "hashtags": [], "htmlContent": "<p>x</p>"}

        light, rows = _split_images(final_post, "post_1")

        assert rows == []
        assert "images" in light and light["images"] == []


class FakeImages:
    """post_images 컬렉션 흉내. 저장된 것만 돌려준다.

    ``$in``도 다룬다 — 목록은 글마다 따로 묻지 않고 한 번에 모아 오기 때문이다.
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.calls: list[dict] = []
        self.cursors: list = []

    def find(self, query, projection=None):
        self.calls.append(query)
        wanted = query["postId"]
        ids = wanted["$in"] if isinstance(wanted, dict) else [wanted]
        rows = [r for r in self._rows if r["postId"] in ids]

        class Cursor:
            def __init__(self):
                self.batch = None

            def batch_size(self, size):
                # 한 번에 받는 장수. 크면 소켓 읽기 한 번이 커져 20초 제한을 넘긴다.
                self.batch = size
                return self

            def __aiter__(self):
                async def go():
                    for row in rows:
                        yield row

                return go()

        cursor = Cursor()
        self.cursors.append(cursor)
        return cursor


@pytest.mark.asyncio
class TestPuttingTheImagesBack:
    def _repo(self, rows: list[dict]) -> MongoBlogTaskRepository:
        return MongoBlogTaskRepository({"blogTask": object(), "post_images": FakeImages(rows)})

    def _document(self) -> dict:
        return {
            "postId": "post_1",
            "finalPost": {
                "title": "제목",
                "body": "본문",
                "hashtags": [],
                "htmlContent": "<p>본문</p>",
                "images": [image_wire(IMAGE_ELSEWHERE)],
            },
        }

    async def test_the_bytes_come_back_for_publishing(self):
        """발행에 넘기는 값은 예전과 똑같아야 한다 — 네이버·스레드 코드는 안 바뀐다."""
        repo = self._repo([{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAAA"}])

        restored = await repo._attach_images(self._document())

        assert restored["finalPost"]["images"][0]["dataUrl"] == "data:image/png;base64,AAAA"

    async def test_an_old_document_is_left_alone(self):
        """이관 전 문서는 바이트를 안에 그대로 들고 있다. 건드리지 않는다."""
        repo = self._repo([])
        document = self._document()
        document["finalPost"]["images"][0]["dataUrl"] = "data:image/png;base64,OLD"

        restored = await repo._attach_images(document)

        assert restored["finalPost"]["images"][0]["dataUrl"] == "data:image/png;base64,OLD"

    async def test_a_missing_image_says_which_post_and_which_one(self):
        """조용히 넘기면 이미지 없는 글이 그대로 발행된다."""
        repo = self._repo([])  # 이미지가 하나도 없다

        with pytest.raises(BlogTaskError) as raised:
            await repo._attach_images(self._document())

        message = str(raised.value)
        assert "post_1" in message
        assert "1번째" in message
        # 왜 멈췄는지가 문구에 있어야 사용자가 다음 행동을 정할 수 있다.
        assert "발행" in message

    async def test_the_text_gets_its_images_back(self):
        """되돌리지 않으면 발행된 글에 이미지 대신 "stored:post_images#0"이 들어간다."""
        repo = self._repo([{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAAA"}])
        document = self._document()
        document["finalPost"]["htmlContent"] = '<img src="stored:post_images#0">'
        document["finalPost"]["markdownContent"] = "![사진](stored:post_images#0)"

        restored = await repo._attach_images(document)

        assert restored["finalPost"]["htmlContent"] == '<img src="data:image/png;base64,AAAA">'
        assert restored["finalPost"]["markdownContent"] == "![사진](data:image/png;base64,AAAA)"

    async def test_broken_base64_padding_in_markdown_is_repaired_on_read(self):
        """형광펜 치환(`==`→`**`)에 base64 패딩이 먹혀 저장된 글의 복구.

        생성 쪽은 2026-08-07에 고쳤지만(markdown_for_storage가 이미지 구간을 보호),
        이미 `…2Q**)` 꼴로 저장된 글은 미리보기 이미지가 깨진 채 남는다 — 읽으면서
        되돌린다.
        """
        from app.modules.blog_task.repository import _repair_markdown_image_padding

        document = {
            "postId": "post_1",
            "finalPost": {
                "markdownContent": (
                    "![썸네일](data:image/jpeg;base64,AAAA/2Q**)\n\n"
                    "본문 **강조**는 그대로 둔다.\n\n"
                    "![사진](data:image/jpeg;base64,BBBB/kUf**)"
                )
            },
        }

        repaired = _repair_markdown_image_padding(document)["finalPost"]["markdownContent"]

        assert "data:image/jpeg;base64,AAAA/2Q==)" in repaired
        assert "data:image/jpeg;base64,BBBB/kUf==)" in repaired
        assert "**강조**" in repaired
        # 멀쩡한 문서는 같은 객체 그대로 돌려준다(복사 비용 없음).
        clean = {"postId": "post_1", "finalPost": {"markdownContent": "본문뿐"}}
        assert _repair_markdown_image_padding(clean) is clean

    async def test_a_round_trip_gives_back_exactly_what_went_in(self):
        """발행에 넘기는 값은 예전과 **글자 하나까지** 같아야 한다."""
        data_url = "data:image/png;base64,AAAA"
        final_post = {
            "title": "제목",
            "body": "본문",
            "hashtags": [],
            "htmlContent": f'<p>앞</p><img src="{data_url}"><p>뒤</p>',
            "markdownContent": f"![사진]({data_url})",
            "images": [image_wire(data_url)],
        }
        light, rows = _split_images(final_post, "post_1")
        repo = self._repo(rows)

        restored = await repo._attach_images({"postId": "post_1", "finalPost": light})

        assert restored["finalPost"]["htmlContent"] == final_post["htmlContent"]
        assert restored["finalPost"]["markdownContent"] == final_post["markdownContent"]
        assert restored["finalPost"]["images"][0]["dataUrl"] == data_url

    async def test_a_missing_image_in_the_text_is_named(self):
        """본문 글에만 자리표가 남은 글도 조용히 넘기지 않는다."""
        repo = self._repo([])
        document = self._document()
        document["finalPost"]["images"] = []
        document["finalPost"]["htmlContent"] = '<img src="stored:post_images#2">'

        with pytest.raises(BlogTaskError) as raised:
            await repo._attach_images(document)

        assert "3번째" in str(raised.value)

    async def test_the_images_come_one_at_a_time(self):
        """한꺼번에 받으면 소켓 읽기 한 번이 그만큼 커진다.

        실측(2026-08-06): 회선이 0.09MB/s여서 이미지 5장(약 2MB)을 한 번에 받으면 22초 —
        `SOCKET_TIMEOUT_MS`(20초)를 넘겨 **글이 아예 안 열렸다**(3건 중 2건 NetworkTimeout).
        한 장이면 0.4MB 남짓이라 5초쯤이다. 총 바이트는 같지만 제한에 걸리지 않는다.
        """
        images = FakeImages(
            [{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAAA"}]
        )
        repo = MongoBlogTaskRepository({"blogTask": object(), "post_images": images})

        await repo._attach_images(self._document())

        assert [c.batch for c in images.cursors] == [1]

    async def test_a_missing_featured_image_is_named_too(self):
        repo = self._repo([{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAAA"}])
        document = self._document()
        document["finalPost"]["featuredImage"] = image_wire(IMAGE_ELSEWHERE)

        with pytest.raises(BlogTaskError, match="대표 이미지"):
            await repo._attach_images(document)


@pytest.mark.asyncio
class TestTheHeavyListPathStillGetsImages:
    """`GET /posts?view=full`은 글 전체를 돌려준다. 화면은 안 쓰지만 계약은 남아 있다.

    이미지를 안 붙이면 `dataUrl` 자리에 표시 문자열이 그대로 나가고, 받는 쪽은 그것을
    이미지 주소로 알고 쓴다 — 깨진 이미지가 뜨고 아무도 이유를 모른다.
    """

    class FakeTasks:
        def __init__(self, documents):
            self._documents = documents

        def find(self, query, projection=None):
            documents = self._documents

            class Cursor:
                def sort(self, *args):
                    return self

                def __aiter__(self):
                    async def go():
                        for doc in documents:
                            yield doc

                    return go()

            return Cursor()

    def _document(self, post_id: str) -> dict:
        return {
            "postId": post_id,
            "userId": "user_1",
            "status": "READY_TO_PUBLISH",
            "version": 1,
            "createdAt": "2026-08-06T00:00:00.000Z",
            "updatedAt": "2026-08-06T00:00:00.000Z",
            "statusHistory": [],
            "postingLogs": [],
            "input": {"topic": "t", "keywords": [], "referenceMaterials": []},
            "finalPost": {
                "title": "제목",
                "body": "본문",
                "hashtags": [],
                "htmlContent": "<p>본문</p>",
                "images": [image_wire(IMAGE_ELSEWHERE)],
            },
        }

    async def test_images_are_fetched_once_for_the_whole_list(self):
        """글마다 따로 물으면 목록 한 번에 쿼리가 글 수만큼 나간다."""
        images = FakeImages(
            [
                {"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAA"},
                {"postId": "post_2", "index": 0, "dataUrl": "data:image/png;base64,BBB"},
            ]
        )
        repo = MongoBlogTaskRepository(
            {
                "blogTask": self.FakeTasks([self._document("post_1"), self._document("post_2")]),
                "post_images": images,
            }
        )

        tasks = await repo.list_by_user_id("user_1")

        # 글이 둘인데 이미지 조회는 한 번이다.
        assert len(images.calls) == 1
        assert "$in" in str(images.calls[0])
        # 그리고 각 글이 자기 이미지를 받았다.
        assert [t.final_post.images[0].data_url for t in tasks] == [
            "data:image/png;base64,AAA",
            "data:image/png;base64,BBB",
        ]

    async def test_a_list_without_stored_images_asks_nothing(self):
        """옛 문서만 있으면 이미지 컬렉션을 부를 이유가 없다."""
        images = FakeImages([])
        document = self._document("post_1")
        document["finalPost"]["images"][0]["dataUrl"] = "data:image/png;base64,OLD"
        repo = MongoBlogTaskRepository(
            {"blogTask": self.FakeTasks([document]), "post_images": images}
        )

        await repo.list_by_user_id("user_1")

        assert images.calls == []

    async def test_the_recovery_sweeper_gets_images_too(self):
        """상태로 훑어 찾아낸 글은 **이어서 발행되는** 글이다.

        서버가 뜰 때 '진행 중인 채로 멈춘' 글을 이 경로로 찾는다. 여기서 이미지를 안
        붙이면 표시 문자열이 이미지 주소인 채로 발행에 넘어간다.
        """
        images = FakeImages(
            [{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAA"}]
        )
        repo = MongoBlogTaskRepository(
            {"blogTask": self.FakeTasks([self._document("post_1")]), "post_images": images}
        )

        tasks = await repo.list_by_status([BlogTaskStatus.READY_TO_PUBLISH])

        assert tasks[0].final_post.images[0].data_url == "data:image/png;base64,AAA"


@pytest.mark.asyncio
class TestWhatComesBackAfterAWrite:
    """상태를 바꾸면 저장소가 바뀐 글을 돌려준다. 저장된 문서에는 **표시만** 있으므로,
    돌려줄 때 붙이지 않으면 그 글로 발행하는 곳에서 표시 문자열이 이미지 주소가 된다.
    """

    class FakeTasks:
        def __init__(self, document: dict):
            self.document = document

        async def find_one_and_update(self, query, update, projection=None, return_document=None):
            return self.document

    async def test_the_task_returned_by_a_write_has_its_images(self):
        images = FakeImages(
            [{"postId": "post_1", "index": 0, "dataUrl": "data:image/png;base64,AAA"}]
        )
        document = {
            "postId": "post_1",
            "userId": "user_1",
            "status": "POSTING",
            "version": 2,
            "createdAt": "2026-08-06T00:00:00.000Z",
            "updatedAt": "2026-08-06T00:00:00.000Z",
            "statusHistory": [],
            "postingLogs": [],
            "input": {"topic": "t", "keywords": [], "referenceMaterials": []},
            "finalPost": {
                "title": "제목",
                "body": "본문",
                "hashtags": [],
                "htmlContent": "<p>본문</p>",
                "images": [image_wire(IMAGE_ELSEWHERE)],
            },
        }
        repo = MongoBlogTaskRepository(
            {"blogTask": self.FakeTasks(document), "post_images": images}
        )
        current = _to_task(dict(document, status="READY_TO_PUBLISH"))

        updated = await repo._apply(current, {"$set": {}})

        assert updated.final_post.images[0].data_url == "data:image/png;base64,AAA"


@pytest.mark.asyncio
class TestDeletingAPostTakesItsImages:
    """안 지우면 주인 없는 이미지가 쌓여, 지운 글의 이미지가 컬렉션을 계속 차지한다."""

    class FakeTasks:
        def __init__(self, deleted: int):
            self._deleted = deleted

        async def delete_one(self, query):
            class Result:
                deleted_count = self._deleted

            return Result()

    class FakeImages:
        def __init__(self):
            self.deleted: list[dict] = []

        async def delete_many(self, query):
            self.deleted.append(query)

    async def test_the_images_go_with_the_post(self):
        images = self.FakeImages()
        repo = MongoBlogTaskRepository(
            {"blogTask": self.FakeTasks(deleted=1), "post_images": images}
        )

        await repo.delete_by_user_and_post_id("user_1", "post_1")

        assert images.deleted == [{"postId": "post_1"}]

    async def test_another_users_images_are_not_touched(self):
        """글이 안 지워졌다는 것은 그 사용자의 글이 아니라는 뜻이다. 이미지도 남의 것이다."""
        images = self.FakeImages()
        repo = MongoBlogTaskRepository(
            {"blogTask": self.FakeTasks(deleted=0), "post_images": images}
        )

        with pytest.raises(BlogTaskError):
            await repo.delete_by_user_and_post_id("남의_사용자", "post_1")

        assert images.deleted == []


@pytest.mark.asyncio
class TestNotFetchingWhatWeThrowAway:
    """읽어 놓고 버리는 필드는 아예 가져오지 않는다.

    `draftCheckpoint`는 원고 생성 중간 저장점이라 `BlogTask` 모델에 없다 — 가져와도
    `_to_task`가 버린다. 그런데 실측에서 한 건이 **2.2MB**였고(2026-08-06), 회선이
    0.09MB/s라 버릴 값을 받는 데만 24초가 걸려 그 글이 안 열렸다.
    """

    class FakeTasks:
        def __init__(self):
            self.projections: list = []

        async def find_one(self, query, projection=None):
            self.projections.append(projection)
            return None

    async def test_the_checkpoint_is_not_fetched_when_opening_a_post(self):
        tasks = self.FakeTasks()
        repo = MongoBlogTaskRepository({"blogTask": tasks, "post_images": FakeImages([])})

        await repo.find_by_user_and_post_id("user_1", "post_1")
        await repo.find_by_post_id("post_1")

        assert tasks.projections == [WITHOUT_UNUSED_FIELDS, WITHOUT_UNUSED_FIELDS]
        assert WITHOUT_UNUSED_FIELDS == {"draftCheckpoint": 0}

    async def test_the_checkpoint_has_its_own_reader(self):
        """빼도 되는 이유는 필요한 곳이 따로 읽기 때문이다."""
        assert hasattr(MongoBlogTaskRepository, "load_draft_checkpoint")


class TestEveryRepositoryKeepsThePromise:
    """저장소 Protocol에 적힌 것을 두 구현이 **다 갖고 있어야 한다.**

    2026-08-06에 병합하면서 `summaries_by_post_ids`가 서비스에만 남고 저장소에서
    사라졌다. 예약 목록은 그것을 부르며 매번 실패했는데, 호출부가 예외를 삼키고
    경고만 찍어서 **화면에는 상태가 비어 보일 뿐 아무도 몰랐다**:

        WARNING: 예약 목록의 글 상태 조회 실패 |
        'MongoBlogTaskRepository' object has no attribute 'summaries_by_post_ids'

    이름 하나가 빠진 것을 사람이 알아채기를 기대하지 않는다.
    """

    def test_both_repositories_implement_the_protocol(self):
        from app.modules.blog_task.repository import (
            BlogTaskRepository,
            InMemoryBlogTaskRepository,
        )

        promised = [
            name
            for name in dir(BlogTaskRepository)
            if not name.startswith("_")
        ]
        for repository in (MongoBlogTaskRepository, InMemoryBlogTaskRepository):
            missing = [name for name in promised if not hasattr(repository, name)]
            assert not missing, f"{repository.__name__}에 없다: {missing}"

    def test_the_service_only_calls_what_the_repositories_have(self):
        """서비스가 부르는 저장소 메서드가 실제로 있는지.

        경로는 **이 파일에서부터** 짚는다. 예전에는 실행 위치를 기준으로 잡아
        (`app/modules/...`) `apps/api`에서 돌릴 때만 통과했다 — README가 안내하는
        `npm test`는 저장소 루트에서 pytest를 부르므로 거기서는 이 테스트가 죽었다.
        """
        import re
        from pathlib import Path

        from app.modules.blog_task.repository import InMemoryBlogTaskRepository

        # tests/ -> apps/api -> app/modules/blog_task/service.py
        service_file = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "modules"
            / "blog_task"
            / "service.py"
        )
        source = service_file.read_text(encoding="utf-8")
        called = set(re.findall(r"self\._repository\.(\w+)", source))
        missing = [name for name in sorted(called) if not hasattr(InMemoryBlogTaskRepository, name)]

        assert not missing, f"서비스가 부르는데 저장소에 없다: {missing}"


@pytest.mark.asyncio
class TestAskingWhoOwnsAPostWithoutReadingIt:
    """`/posts` 아래 모든 동작 앞에 붙는 소유권 검사.

    글을 통째로 읽지 않는다 — 한 편이 몇 MB라, 예전에는 '선택 삭제'로 24편을 지우면
    수십 MB가 오가느라 끝나지 않았다.
    """

    class FakeTasks:
        def __init__(self, owner: str | None):
            self.owner = owner
            self.projections: list = []

        async def find_one(self, query, projection=None):
            self.projections.append(projection)
            if self.owner is None or query.get("userId") != self.owner:
                return None
            return {"_id": "x"}

    async def test_the_owner_gets_true(self):
        tasks = self.FakeTasks(owner="user_1")
        repo = MongoBlogTaskRepository({"blogTask": tasks, "post_images": FakeImages([])})

        assert await repo.owns_post("user_1", "post_1") is True

    async def test_someone_else_gets_false(self):
        """없는 글도 남의 글도 같은 답이다 — 호출부가 둘 다 404로 만든다."""
        tasks = self.FakeTasks(owner="user_1")
        repo = MongoBlogTaskRepository({"blogTask": tasks, "post_images": FakeImages([])})

        assert await repo.owns_post("남의_사용자", "post_1") is False

    async def test_it_does_not_pull_the_post(self):
        tasks = self.FakeTasks(owner="user_1")
        repo = MongoBlogTaskRepository({"blogTask": tasks, "post_images": FakeImages([])})

        await repo.owns_post("user_1", "post_1")

        # _id 하나만 받는다. 원고·이미지가 딸려 오면 안 된다.
        assert tasks.projections == [{"_id": 1}]
