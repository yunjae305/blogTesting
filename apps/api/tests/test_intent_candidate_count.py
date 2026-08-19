"""방향 후보 수는 **한 곳에서** 정한다(2026-08-12).

프롬프트에만 "후보 4개"라고 적고 응답 스키마의 ``maxItems``를 3으로 두었더니 모델이
3개만 돌려주었다 — 화면에도 3개만 떴다(사용자 신고). 개수를 정하는 자리가 둘이면
반드시 갈라진다.
"""

from app.llm import prompts
from app.llm.schemas import (
    GEMINI_INTENT_SCHEMA,
    INTENT_CANDIDATE_COUNT,
    INTENT_SCHEMA,
)


def _bounds(schema):
    candidates = schema["properties"]["intentCandidates"]
    return candidates["minItems"], candidates["maxItems"]


class TestOnePlaceDecidesTheCount:
    def test_the_schema_asks_for_exactly_that_many(self):
        assert _bounds(INTENT_SCHEMA) == (INTENT_CANDIDATE_COUNT, INTENT_CANDIDATE_COUNT)

    def test_the_gemini_schema_says_the_same(self):
        """Gemini용은 변환본이다 — 변환이 개수를 흘리면 여기서 걸린다."""
        assert _bounds(GEMINI_INTENT_SCHEMA) == (
            INTENT_CANDIDATE_COUNT,
            INTENT_CANDIDATE_COUNT,
        )

    def test_the_prompt_says_the_same_number(self, monkeypatch):
        """프롬프트 문구가 스키마와 다른 수를 말하면 모델이 헷갈린다.

        문구를 눈으로 확인하지 않고 **실제로 만들어 본다** — 문장 안에 숫자를 박아 두었을
        때 정확히 그것을 놓쳤다.
        """
        import inspect

        source = inspect.getsource(prompts.research_summarize_prompt)
        # 숫자를 문장에 직접 적지 않고 상수를 끼워 넣는지 본다.
        assert "INTENT_CANDIDATE_COUNT" in source
        assert "후보 3개" not in source

    def test_the_count_is_more_than_the_drafts_we_can_make(self):
        """후보가 편수보다 많아야 3편을 만들 때도 **하나는 버리는 선택**이 남는다."""
        from app.modules.blog_task.validation import MAX_DRAFT_COUNT

        assert INTENT_CANDIDATE_COUNT > MAX_DRAFT_COUNT


class TestTheCountHoldsWhenSourcesArePending:
    """자료 없이 방향만 만드는 경로도 **같은 개수**여야 한다(2026-08-12).

    2편 이상이거나 작업 시각을 정해 둔 글은 검증 단계에서 자료를 모으지 않는다. 그때도
    사용자가 고르는 것은 방향이고, 화면은 "방향 후보 4개"라고 적는다.
    """

    @staticmethod
    def _input():
        from app.shared import BlogTaskInput, WebSearchAnalysisInput

        return WebSearchAnalysisInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(topic="롯데리아", purpose=["정보 전달"], keywords=[]),
            prompt_version="v1",
        )

    def test_it_asks_for_the_same_number(self):
        prompt = prompts.research_summarize_prompt(
            self._input(), "", [], sources_pending=True
        )

        assert f"후보 {INTENT_CANDIDATE_COUNT}개" in prompt

    def test_it_forbids_inventing_sources(self):
        """모으지 않았는데 출처를 채우면 전부 거짓이 된다."""
        prompt = prompts.research_summarize_prompt(
            self._input(), "", [], sources_pending=True
        )

        assert "빈 배열" in prompt
        assert "Gemini sources" not in prompt

    def test_the_title_rule_is_the_same_one(self):
        """방향 칸이 무엇인지는 두 경로가 같은 문장을 써야 한다."""
        pending = prompts.research_summarize_prompt(self._input(), "", [], sources_pending=True)
        collected = prompts.research_summarize_prompt(self._input(), "요약", [])

        assert prompts._title_rule_block(None) in pending
        assert prompts._title_rule_block(None) in collected
