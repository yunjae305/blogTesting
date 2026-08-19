"""Opus 5 요청 봉투와 stop_reason 처리.

여기서 막는 것: Opus 5가 400으로 거절하는 필드(temperature·top_p·top_k)가 어느 한 경로에만
남는 것, 허용되지 않는 effort·thinking 조합이 요청으로 나가는 것, 그리고 HTTP 200으로 온
쓸 수 없는 응답(잘림·거절·컨텍스트 초과)이 조용히 파서로 흘러가는 것.
"""

import json
import logging

import httpx
import pytest
import respx

from app.llm import live_adapters
from app.llm.live_adapters import (
    STAGE_BUDGETS,
    AnthropicDraftGenerator,
    AnthropicTopicGenerator,
    anthropic_request_body,
    check_anthropic_stop_reason,
)
from app.llm.parsing import (
    ProviderContextExceededError,
    ProviderRefusedError,
    ProviderTruncatedError,
)
from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
from app.llm.schemas import TOPIC_SCHEMA

ANTHROPIC = "https://api.anthropic.com/v1/messages"


def role(which: LlmRole = LlmRole.M2_TOPIC) -> RoleConfig:
    return RoleConfig(
        role=which,
        label=which.value,
        provider=LlmProvider.ANTHROPIC,
        model="claude-opus-5",
        api_key_env="ANTHROPIC_API_KEY",
        api_key="test-key",
        has_credentials=True,
    )


def body_for(stage: str = "m2-topic") -> dict:
    return anthropic_request_body(
        model="claude-opus-5",
        stage=stage,
        system="system",
        content="prompt",
        tool_name="return_title_candidates",
        tool_description="desc",
        tool_schema=TOPIC_SCHEMA,
    )


class TestRequestEnvelope:
    def test_the_three_sampling_fields_are_never_built(self):
        # 제거가 아니라 미생성이다 — 400을 받은 뒤 빼고 재호출하는 우회 경로를 두지 않는다.
        for stage in STAGE_BUDGETS:
            body = body_for(stage)
            assert "temperature" not in body, stage
            assert "top_p" not in body, stage
            assert "top_k" not in body, stage

    def test_every_stage_declares_an_effort(self):
        for stage in STAGE_BUDGETS:
            assert body_for(stage)["output_config"]["effort"] in {
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            }

    def test_adaptive_thinking_is_expressed_by_omitting_the_field(self):
        # Opus 5는 thinking이 기본 ON이고 {"type":"adaptive"}가 기본값과 같다. 생략이 곧
        # adaptive이고, 그래야 이 단정이 의도를 그대로 표현한다.
        for stage, budget in STAGE_BUDGETS.items():
            if budget.thinking == "adaptive":
                assert "thinking" not in body_for(stage), stage

    def test_the_tool_is_forced_so_the_answer_comes_back_as_structured_data(self):
        body = body_for()
        assert body["tool_choice"] == {"type": "tool", "name": "return_title_candidates"}
        assert [tool["name"] for tool in body["tools"]] == ["return_title_candidates"]

    def test_a_json_format_is_merged_into_the_same_output_config(self):
        # 실측: output_config는 effort와 format을 함께 받는다(HTTP 200).
        body = anthropic_request_body(
            model="claude-opus-5",
            stage="m2-topic",
            system="system",
            content="prompt",
            tool_name="t",
            tool_description="d",
            tool_schema=TOPIC_SCHEMA,
            output_format={"type": "json_schema", "schema": {"type": "object"}},
        )
        assert set(body["output_config"]) == {"effort", "format"}


class TestEveryStageHasABudget:
    """호출부가 쓰는 stage 이름이 **모두** STAGE_BUDGETS에 있어야 한다.

    없으면 `anthropic_tool_call`의 첫 줄 `STAGE_BUDGETS[stage]`가 KeyError를 낸다.
    그리고 그것이 조용히 지나갈 수 있다 — 문장 다듬기(M4 5단계)는 호출부가 실패를
    삼키고 원고를 그대로 쓰도록 되어 있어서(그 설계는 맞다), `m4-polish` 항목이
    빠진 채로 **한 번도 돌지 않았다.** 로그에만 `KeyError: 'm4-polish'`가 남았다
    (2026-08-06 사용자 로그).

    그래서 사람이 목록을 맞춰 보는 대신 소스에서 뽑아 대조한다.
    """

    def test_소스의_모든_stage가_예산을_갖는다(self):
        import re
        from pathlib import Path

        source = Path(live_adapters.__file__).read_text(encoding="utf-8")
        # `stage="m4-polish"` 처럼 리터럴로 넘기는 자리만 본다(변수는 여기서 못 본다).
        used = set(re.findall(r'stage="([a-z0-9-]+)"', source))
        # 테스트가 심는 가짜 이름은 제외한다.
        used.discard("_probe")

        missing = sorted(used - set(STAGE_BUDGETS))
        assert not missing, f"STAGE_BUDGETS에 빠진 단계: {missing}"

    def test_문장_다듬기_단계가_예산을_갖는다(self):
        """위 테스트가 잡는 것을 이름으로도 한 번 더 못 박는다 — 이 단계가 실제로
        빠져 있었고, 실패가 조용해서 오래 눈에 띄지 않았다."""
        assert "m4-polish" in STAGE_BUDGETS
        assert body_for("m4-polish")["max_tokens"] > 0


class TestForbiddenCombinations:
    def test_disabled_thinking_with_xhigh_effort_is_refused_before_the_request(self):
        # 실측 응답: "output_config.effort 'xhigh' is not supported when thinking is disabled".
        # 400을 받고 나서 알아내는 대신 조립 단계에서 막는다.
        STAGE_BUDGETS["_probe"] = live_adapters.StageBudget(
            effort="xhigh", max_tokens=1000, thinking="disabled"
        )
        try:
            with pytest.raises(ValueError, match="high 이하"):
                body_for("_probe")
        finally:
            del STAGE_BUDGETS["_probe"]

    def test_disabled_thinking_with_high_effort_is_allowed(self):
        STAGE_BUDGETS["_probe"] = live_adapters.StageBudget(
            effort="high", max_tokens=1000, thinking="disabled"
        )
        try:
            assert body_for("_probe")["thinking"] == {"type": "disabled"}
        finally:
            del STAGE_BUDGETS["_probe"]

    def test_an_unknown_effort_is_refused(self):
        STAGE_BUDGETS["_probe"] = live_adapters.StageBudget(effort="turbo", max_tokens=1000)
        try:
            with pytest.raises(ValueError, match="effort는"):
                body_for("_probe")
        finally:
            del STAGE_BUDGETS["_probe"]

    def test_a_zero_max_tokens_is_refused(self):
        STAGE_BUDGETS["_probe"] = live_adapters.StageBudget(effort="low", max_tokens=0)
        try:
            with pytest.raises(ValueError, match="max_tokens"):
                body_for("_probe")
        finally:
            del STAGE_BUDGETS["_probe"]


class TestStopReason:
    def test_a_truncated_answer_is_not_a_parsing_error(self):
        with pytest.raises(ProviderTruncatedError) as caught:
            check_anthropic_stop_reason(
                {"stop_reason": "max_tokens"},
                stage="m4-draft",
                model="claude-opus-5",
                max_tokens=32000,
            )
        assert caught.value.max_tokens == 32000
        assert caught.value.stage == "m4-draft"

    def test_a_refusal_carries_its_category(self):
        with pytest.raises(ProviderRefusedError) as caught:
            check_anthropic_stop_reason(
                {"stop_reason": "refusal", "stop_details": {"category": "policy_violation"}},
                stage="m2-topic",
                model="claude-opus-5",
                max_tokens=6000,
            )
        assert caught.value.category == "policy_violation"

    def test_a_context_overflow_is_its_own_error(self):
        with pytest.raises(ProviderContextExceededError):
            check_anthropic_stop_reason(
                {"stop_reason": "model_context_window_exceeded", "stop_details": None},
                stage="m4-draft",
                model="claude-opus-5",
                max_tokens=32000,
            )

    def test_normal_stop_reasons_pass_through(self):
        for reason in ("tool_use", "end_turn", "stop_sequence", "pause_turn", None):
            check_anthropic_stop_reason(
                {"stop_reason": reason}, stage="m2-topic", model="m", max_tokens=1
            )


class TestRealRequests:
    """실제 어댑터가 보내는 본문. 조립 함수만 보면 호출부가 그것을 쓰는지 알 수 없다."""

    @respx.mock
    async def test_the_title_request_carries_effort_and_no_temperature(self):
        route = respx.post(ANTHROPIC).mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "return_title_candidates",
                            "input": {
                                "topicCandidates": [
                                    {
                                        "title": "장마철 제습기 관리 순서를 정리했습니다",
                                        "titleType": "정보형",
                                        "hookType": "NONE",
                                        "hookStrength": "LOW",
                                    }
                                ]
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            )
        )
        from app.llm.contracts import TopicGenerationInput
        from app.shared import BlogTaskInput, TrendKeyword, TrendSource

        await AnthropicTopicGenerator(role()).generate_topics(
            TopicGenerationInput(
                post_id="p",
                input=BlogTaskInput(topic="제습기", keywords=["제습기"]),
                trend_keyword=TrendKeyword(
                    trend_keyword_id="k",
                    keyword="장마",
                    source=TrendSource.NAVER_DATALAB,
                    rank=1,
                    score=1.0,
                    collected_at="2026-07-30T00:00:00Z",
                ),
            )
        )
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "claude-opus-5"
        assert body["output_config"] == {"effort": "high"}
        assert body["max_tokens"] == STAGE_BUDGETS["m2-topic"].max_tokens
        assert "temperature" not in body
        assert "thinking" not in body

    @respx.mock
    async def test_a_truncated_draft_raises_instead_of_returning_half_a_post(self):
        respx.post(ANTHROPIC).mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "return_blog_draft",
                            "input": {"finalPost": {"title": "잘린"}},
                        }
                    ],
                    "stop_reason": "max_tokens",
                },
            )
        )
        with pytest.raises(ProviderTruncatedError):
            await AnthropicDraftGenerator(role(LlmRole.M4_DRAFT))._complete_json(
                "system", "prompt"
            )


class TestEmptyBodyDiagnostic:
    """실사례(2026-08-03): 응답 6,476바이트·출력 2,696토큰으로 성공했는데 body_chars=0이었다.

    잘린 응답이 아니라(그건 stop_reason이 잡는다) 파서가 본문을 못 꺼낸 것인데, 원본 응답을
    어디에도 남기지 않아 원인을 좁힐 단서가 없었다. 그 시도는 통째로 버려지고 재생성이 돈다.
    """

    def test_an_empty_body_logs_the_response_shape(self, caplog):
        from app.llm.live_adapters import _log_empty_body
        from app.shared import FinalPost

        empty = FinalPost(
            title="제목", body="", hashtags=[], html_content="", markdown_content=""
        )
        with caplog.at_level(logging.WARNING, logger="app.llm.live_adapters"):
            _log_empty_body(
                {"markdownContent": ["조각1", "조각2"], "title": "제목"}, empty, "post_1"
            )

        assert "원고 본문이 비어 파싱됨" in caplog.text
        # 어떤 키가 어떤 타입·길이로 왔는지가 진단의 전부다.
        assert "markdownContent=list(2)" in caplog.text
        # 본문 자체는 절대 로그에 싣지 않는다.
        assert "조각1" not in caplog.text

    def test_a_normal_body_logs_nothing(self, caplog):
        from app.llm.live_adapters import _log_empty_body
        from app.shared import FinalPost

        good = FinalPost(
            title="제목",
            body="본문이 있습니다.",
            hashtags=[],
            html_content="<p>본문이 있습니다.</p>",
            markdown_content="본문이 있습니다.",
        )
        with caplog.at_level(logging.WARNING, logger="app.llm.live_adapters"):
            _log_empty_body({"markdownContent": "본문이 있습니다."}, good, "post_1")

        assert caplog.text == ""
