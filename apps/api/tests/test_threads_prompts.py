"""스레드 전용 원고 프롬프트 — 블로그 문법이 섞이지 않는 짧은 글.

생성기는 분리돼 있다: 블로그의 SEO·소제목·썸네일 규칙이 스레드 글에 새면 안 되고,
스레드 글의 근거는 검증을 마친 블로그 본문에서만 온다.
"""

import pytest

from app.llm.threads_prompts import (
    THREADS_POST_SYSTEM_PROMPT,
    THREADS_POST_TEXT_LIMIT,
    thread_plan_for,
    threads_post_from_json,
    threads_post_prompt,
)
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    FinalPost,
    SelectedIntent,
)

NOW = "1970-01-01T00:00:00.000Z"


def build_task() -> BlogTask:
    return BlogTask(
        post_id="post_1",
        user_id="user_1",
        status=BlogTaskStatus.READY_TO_PUBLISH,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        status_history=[],
        input=BlogTaskInput(
            topic="스레드 글쓰기",
            keywords=["훅", "구조"],
            target_reader="1인 창작자",
            reference_materials=[],
        ),
        selected_intent=SelectedIntent(
            intent_id="intent_1",
            title="스레드에서 읽히는 글의 구조",
            target_reader="1인 창작자",
            rationale="구조 관점",
            keywords=["첫 문장", "체류 시간"],
        ),
        final_post=FinalPost(
            title="블로그 제목",
            body="첫 문단 사실입니다. [[IMAGE: scene]] 둘째 문단 사실입니다. [[VISUAL: chart-1]]",
            hashtags=["블로그"],
            html_content="<article><h1>블로그 제목</h1></article>",
        ),
        posting_logs=[],
    )


class TestThreadsPostPrompt:
    def test_the_system_prompt_bans_blog_shapes(self):
        for banned in ("제목", "목차", "소제목", "썸네일", "해시태그"):
            assert banned in THREADS_POST_SYSTEM_PROMPT, banned
        # 첫 스레드가 훅이라는 핵심 규칙.
        assert "첫 스레드는 반드시 훅" in THREADS_POST_SYSTEM_PROMPT
        # 개수를 모델이 정하지 않는다는 것이 이 프롬프트의 핵심 규칙이다.
        assert "스스로 정하지 않는다" in THREADS_POST_SYSTEM_PROMPT

    def test_the_prompt_carries_topic_intent_and_verified_facts(self):
        prompt = threads_post_prompt(build_task())

        assert "스레드 글쓰기" in prompt
        assert "첫 문단 사실입니다." in prompt
        # 이미지·시각자료 마커는 근거 블록에 실리지 않는다.
        assert "[[" not in prompt
        # 블로그 문체를 베끼지 말라는 지시가 근거 블록에 붙는다.
        assert "문체" in prompt

    def test_a_task_without_a_final_post_still_builds_a_prompt(self):
        task = build_task().model_copy(update={"final_post": None})

        prompt = threads_post_prompt(task)

        assert "스레드 글쓰기" in prompt
        assert "근거 자료" not in prompt


class TestTheLengthSettingFixesTheThreadCount:
    """2026-08-04 사용자 결정: 개수·글자 수·역할을 설정이 정하고 모델은 채우기만 한다."""

    def test_short_asks_for_two_or_three(self):
        prompt = threads_post_prompt(build_task(), "short")

        assert "2~3개" in prompt
        assert "300~550자" in prompt
        for role in ("훅", "핵심 내용", "정리"):
            assert role in prompt, role

    def test_medium_asks_for_three_to_five(self):
        prompt = threads_post_prompt(build_task(), "medium")

        assert "3~5개" in prompt
        assert "600~1000자" in prompt
        # 중간의 고정 구조가 그대로 실린다.
        for role in ("훅", "배경", "핵심 내용", "덧붙일", "정리"):
            assert role in prompt, role

    def test_it_is_not_a_sales_post(self):
        """원고를 나눠 싣는 글이지 광고 글이 아니다.

        처음 규격을 받을 때 예시가 쿠팡 파트너스 상품 글이라 CTA(링크) 구조를 넣었다가
        용도 확인 뒤 걷어냈다(2026-08-04). 다시 들어오지 않게 막는다.
        """
        prompt = threads_post_prompt(build_task(), "short")

        assert "CTA" not in prompt
        assert "광고 문구·구매 유도·링크는 넣지 마세요" in prompt
        assert "원고에 없는 이야기를 새로 만들지 마세요" in prompt
        assert "CTA" not in THREADS_POST_SYSTEM_PROMPT

    def test_an_unknown_length_falls_back_to_medium(self):
        """저장된 옛 값 "long"이 남아 있을 수 있다 — KeyError로 발행이 죽으면 안 된다."""
        assert thread_plan_for("long").label == "중간"
        assert thread_plan_for(None).label == "중간"
        assert thread_plan_for("").label == "중간"

    def test_the_model_is_told_not_to_number_the_threads(self):
        """번호를 직접 쓰면 순서가 바뀔 때 본문과 어긋난다 — order 필드가 순서를 정한다."""
        assert "번호" in threads_post_prompt(build_task(), "short")


class TestThreadsPostFromJson:
    def test_an_ordered_list_passes_through(self):
        parsed = {
            "threads": [
                {"order": 1, "content": "훅입니다."},
                {"order": 2, "content": "핵심입니다."},
                {"order": 3, "content": "CTA입니다."},
            ]
        }

        assert threads_post_from_json(parsed, "short") == ["훅입니다.", "핵심입니다.", "CTA입니다."]

    def test_out_of_order_items_are_sorted_by_order(self):
        """order가 곧 게시 순서다 — 배열 순서를 믿으면 훅과 CTA가 뒤집힌다."""
        parsed = {
            "threads": [
                {"order": 3, "content": "CTA입니다."},
                {"order": 1, "content": "훅입니다."},
                {"order": 2, "content": "핵심입니다."},
            ]
        }

        assert threads_post_from_json(parsed, "short") == ["훅입니다.", "핵심입니다.", "CTA입니다."]

    def test_an_empty_list_is_an_error_not_a_blank_publish(self):
        with pytest.raises(ValueError):
            threads_post_from_json({"threads": []}, "short")
        with pytest.raises(ValueError):
            threads_post_from_json(None, "short")
        with pytest.raises(ValueError):
            threads_post_from_json({"threads": [{"order": 1, "content": "  "}]}, "short")

    def test_too_few_threads_is_refused(self):
        """구조(훅 → 내용 → CTA)가 깨진 결과라 코드가 메울 수 없다."""
        parsed = {"threads": [{"order": 1, "content": "하나뿐입니다."}]}

        with pytest.raises(ValueError):
            threads_post_from_json(parsed, "short")

    def test_too_many_threads_are_cut_from_the_back(self):
        """앞에서부터 훅 → 내용 순이라 앞쪽이 더 중요하다."""
        parsed = {
            "threads": [{"order": n, "content": f"{n}번입니다."} for n in range(1, 7)]
        }

        result = threads_post_from_json(parsed, "short")

        assert len(result) == 3  # 짧게 상한
        assert result[0] == "1번입니다."

    def test_one_over_limit_thread_is_cut_at_a_paragraph_boundary(self):
        paragraphs = [f"{n}번 문단입니다. 한도를 넘기기 위한 채움 문장입니다." for n in range(1, 30)]
        long_one = "\n\n".join(paragraphs)
        parsed = {
            "threads": [
                {"order": 1, "content": long_one},
                {"order": 2, "content": "둘째입니다."},
                {"order": 3, "content": "셋째입니다."},
            ]
        }

        result = threads_post_from_json(parsed, "short")

        assert len(result[0]) <= THREADS_POST_TEXT_LIMIT
        assert result[0].endswith("문장입니다.")  # 문단 중간에서 뚝 끊기지 않는다

    def test_the_limit_matches_the_publisher_limit(self):
        """프롬프트 계층과 발행 계층이 다른 한도를 보면 한쪽에서 자른 글이 다른 쪽에서 거절된다."""
        from app.posting.threads_split import THREAD_TEXT_LIMIT

        assert THREADS_POST_TEXT_LIMIT == THREAD_TEXT_LIMIT
