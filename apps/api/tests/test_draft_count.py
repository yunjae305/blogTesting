"""한 소재로 여러 편(2026-08-12 사용자 결정).

새 글 작성이 예약을 흡수하면서, 예약 화면이 하던 "하나의 소재로 여러 편"도 이쪽으로
옮겨 왔다. 화면은 −·+로 1~3편을 받는다.

여기서 지키는 것은 둘이다: **옛 문서·옛 요청이 그대로 읽힌다**는 것과, 상한을 넘는 값이
서버에서 막힌다는 것.
"""

import pytest

from app.modules.blog_task.validation import (
    MAX_DRAFT_COUNT,
    validate_blog_task_input,
    validate_draft_count,
)
from app.shared import BlogTaskInput
from app.errors import BlogTaskError

BASE = {
    "topic": "소재",
    "purpose": ["정보 전달"],
    "audience": "일반",
    "tone": "친근",
    "category": "IT",
    "length": "보통",
}


class TestTheCountIsOptional:
    def test_an_old_request_without_the_field_makes_one_draft(self):
        """편수를 모르던 화면이 보낸 요청도 그대로 통과한다."""
        assert validate_blog_task_input(BASE).draft_count == 1

    def test_an_old_document_without_the_field_still_loads(self):
        """저장된 옛 글에는 이 칸이 아예 없다 — 기본값이 그것을 메운다."""
        assert BlogTaskInput(topic="옛 글", keywords=["정보 전달"]).draft_count == 1

    def test_the_field_rides_through_validation(self):
        assert validate_blog_task_input({**BASE, "draftCount": 3}).draft_count == 3


class TestTheCountHasABound:
    @pytest.mark.parametrize("bad", [0, -1, MAX_DRAFT_COUNT + 1, 10])
    def test_out_of_range_is_refused(self, bad):
        with pytest.raises(BlogTaskError):
            validate_draft_count(bad)

    @pytest.mark.parametrize("bad", [True, "2", 2.0, [2]])
    def test_a_value_that_is_not_a_whole_number_is_refused(self, bad):
        """참(True)은 int의 부분형이라 그냥 두면 1로 통과한다 — 화면의 실수다."""
        with pytest.raises(BlogTaskError):
            validate_draft_count(bad)

    def test_the_client_and_the_server_agree_on_the_bound(self):
        """화면 상수와 어긋나면 화면은 통과시키고 서버가 거부한다.

        경로는 **이 파일에서부터** 짚는다. 예전에는 실행 위치를 기준으로 잡아
        (`../web/src/constants.ts`) `apps/api`에서 돌릴 때만 통과했다 — README가 안내하는
        `npm test`는 저장소 루트에서 pytest를 부르므로 거기서는 이 테스트가 죽었다.
        어디서 돌리든 같은 파일을 봐야 한다.
        """
        from pathlib import Path

        # tests/ -> apps/api -> apps -> apps/web/src/constants.ts
        source = Path(__file__).resolve().parents[2] / "web" / "src" / "constants.ts"
        text = source.read_text(encoding="utf-8")
        assert f"MAX_DRAFT_COUNT = {MAX_DRAFT_COUNT};" in text


class TestChoosingSeveralDirections:
    """2편 이상이면 방향도 그만큼 고른다(2026-08-12). 후보는 4개다."""

    def test_one_draft_sends_no_extra_directions(self):
        """한 편이면 예전과 완전히 같은 요청이다."""
        from app.modules.blog_task.validation import validate_select_intent_request

        assert validate_select_intent_request({"intentId": "a"}).additional_intent_ids == []

    def test_the_order_chosen_is_the_order_kept(self):
        """고른 차례가 만들어지는 차례다 — 정렬하거나 뒤섞지 않는다."""
        from app.modules.blog_task.validation import validate_select_intent_request

        parsed = validate_select_intent_request(
            {"intentId": "a", "additionalIntentIds": ["c", "b"]}
        )

        assert parsed.intent_id == "a"
        assert parsed.additional_intent_ids == ["c", "b"]

    @pytest.mark.parametrize(
        "extra",
        [
            ["a"],  # 첫 방향과 같다
            ["b", "b"],  # 서로 같다
        ],
    )
    def test_the_same_direction_twice_is_refused(self, extra):
        """같은 방향으로 두 편을 쓰면 말만 바꾼 중복 글이 된다."""
        from app.modules.blog_task.validation import validate_select_intent_request

        with pytest.raises(BlogTaskError):
            validate_select_intent_request({"intentId": "a", "additionalIntentIds": extra})

    def test_more_than_the_bound_is_refused(self):
        """첫 방향을 포함해 MAX_DRAFT_COUNT를 넘을 수 없다."""
        from app.modules.blog_task.validation import validate_select_intent_request

        extra = [f"x{index}" for index in range(MAX_DRAFT_COUNT)]

        with pytest.raises(BlogTaskError):
            validate_select_intent_request({"intentId": "a", "additionalIntentIds": extra})


class TestAutoPublish:
    """원고가 끝나면 그대로 올릴지(2026-08-12 사용자 요청).

    기본은 켬이다 — 예약 작업은 여태 원고를 만들면 바로 올렸고, 그 동작을 바꾸면 이미
    걸어 둔 예약의 뜻이 달라진다.
    """

    def test_not_saying_anything_publishes_to_naver_only(self):
        """플랫폼마다 따로 고르게 바뀌었다(2026-08-12) — 기본은 예전 그대로 네이버뿐이다."""
        parsed = validate_blog_task_input(BASE)

        assert parsed.auto_publish_naver is True
        assert parsed.auto_publish_threads is False

    def test_an_old_document_publishes_to_naver_too(self):
        task = BlogTaskInput(topic="옛 글", keywords=["k"])

        assert task.auto_publish_naver is True
        assert task.auto_publish_threads is False

    def test_turning_everything_off_is_refused(self):
        """**올릴 곳은 하나 이상이어야 한다**(2026-08-13 사용자 지시).

        옛 화면의 통짜 `autoPublish: False`도 여기 걸린다. 그 요청을 받아 주던 시절에는
        아무 데도 올라가지 않는 글이 작업 큐에 서서, 화면·진행바·로그가 전부 그 예외를
        설명해야 했다. 예약 화면·재예약은 예전부터 같은 규칙이었다.
        """
        with pytest.raises(BlogTaskError) as caught:
            validate_blog_task_input({**BASE, "autoPublish": False})

        assert caught.value.code == "VALIDATION_FAILED"
        assert "하나 이상" in caught.value.message

    def test_turning_both_platforms_off_is_refused(self):
        with pytest.raises(BlogTaskError):
            validate_blog_task_input(
                {**BASE, "autoPublishNaver": False, "autoPublishThreads": False}
            )

    def test_each_platform_is_chosen_on_its_own(self):
        parsed = validate_blog_task_input(
            {**BASE, "autoPublishNaver": False, "autoPublishThreads": True}
        )

        assert parsed.auto_publish_naver is False
        assert parsed.auto_publish_threads is True

    @pytest.mark.parametrize("field", ["autoPublish", "autoPublishNaver", "autoPublishThreads"])
    @pytest.mark.parametrize("bad", ["false", 0, [], {}])
    def test_a_value_that_is_not_a_boolean_is_refused(self, field, bad):
        with pytest.raises(BlogTaskError):
            validate_blog_task_input({**BASE, field: bad})
