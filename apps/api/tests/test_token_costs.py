"""토큰 단가·비용 터미널 표시 회귀 테스트.

perf.record_provider_call이 남기는 "토큰 사용" 줄과 trace.finish()의 "토큰 요약" 표가
이 기능의 전부다 — 로그 형식이 조용히 깨지면 사용자가 보는 화면이 깨진다.
"""

import logging
import time

import pytest

from app.shared import perf
from app.shared.token_costs import estimate_cost_usd, price_for


class TestTokenCosts:
    def test_등록된_모델은_입출력_단가로_비용을_계산한다(self):
        # claude-opus-5: 입력 $5/1M, 출력 $25/1M (2026-08-11 확인)
        assert estimate_cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(
            30.0
        )

    def test_날짜_고정_snapshot은_기본_모델과_같은_단가다(self):
        assert price_for("claude-opus-5-20260101") == price_for("claude-opus-5")

    def test_미등록_모델은_비용이_None이다(self):
        # gpt-image-2는 토큰이 아니라 장당 과금이라 표에 없다.
        assert estimate_cost_usd("gpt-image-2", 100, 100) is None

    def test_토큰_정보가_전혀_없으면_None이다(self):
        assert estimate_cost_usd("claude-opus-5", None, None) is None

    def test_한쪽_토큰만_보고돼도_그만큼은_계산한다(self):
        assert estimate_cost_usd("claude-opus-5", None, 1_000_000) == pytest.approx(25.0)


def _call(model: str, provider: str, input_tokens, output_tokens) -> None:
    perf.record_provider_call(
        provider=provider,
        model=model,
        start=time.monotonic() - 0.01,
        end=time.monotonic(),
        status=200,
        attempts=1,
        response_bytes=10,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class TestTokenLogging:
    def test_trace_없이도_호출별_토큰_로그가_남는다(self, caplog):
        # M2 경로는 trace를 시작하지 않는다 — 그래도 토큰·비용은 보여야 한다.
        perf.current_trace.set(None)
        with caplog.at_level(logging.INFO, logger="app.perf"):
            _call("claude-opus-5", "anthropic", 1_000, 500)
        lines = [r.getMessage() for r in caplog.records if "토큰 사용" in r.getMessage()]
        assert len(lines) == 1
        assert "AI=anthropic/claude-opus-5" in lines[0]
        assert "입력=1,000" in lines[0]
        assert "출력=500" in lines[0]
        assert "비용=$" in lines[0]

    def test_미등록_모델은_단가_미등록으로_표시한다(self, caplog):
        perf.current_trace.set(None)
        with caplog.at_level(logging.INFO, logger="app.perf"):
            _call("gpt-image-2", "openai", 200, 100)
        lines = [r.getMessage() for r in caplog.records if "토큰 사용" in r.getMessage()]
        assert len(lines) == 1
        assert "비용=단가 미등록" in lines[0]

    def test_단계_이름은_한글로_표시한다(self, caplog):
        # span 이름은 코드 식별자로 남고, 토큰 로그에만 한글 표기가 나온다.
        perf.current_trace.set(None)
        with caplog.at_level(logging.INFO, logger="app.perf"):
            with perf.span("draft_llm_attempt_1"):
                _call("claude-opus-5", "anthropic", 100, 100)
            with perf.span("dual_critique"):
                _call("gpt-5.6-sol", "openai", 100, 100)
        lines = [r.getMessage() for r in caplog.records if "토큰 사용" in r.getMessage()]
        assert "단계=본문 작성 1차" in lines[0]
        assert "단계=이중 비평" in lines[1]

    def test_모르는_단계_이름은_그대로_보여_준다(self, caplog):
        perf.current_trace.set(None)
        with caplog.at_level(logging.INFO, logger="app.perf"):
            with perf.span("낯선_새_단계"):
                _call("claude-opus-5", "anthropic", 100, 100)
        lines = [r.getMessage() for r in caplog.records if "토큰 사용" in r.getMessage()]
        assert "단계=낯선_새_단계" in lines[0]

    def test_usage가_없는_호출은_토큰_줄을_만들지_않는다(self, caplog):
        perf.current_trace.set(None)
        with caplog.at_level(logging.INFO, logger="app.perf"):
            _call("claude-opus-5", "anthropic", None, None)
        assert not [r for r in caplog.records if "토큰 사용" in r.getMessage()]

    def test_finish가_단계별_토큰_요약과_합계를_남긴다(self, caplog):
        trace = perf.start_trace("m4-draft", "post_1")
        try:
            with caplog.at_level(logging.INFO, logger="app.perf"):
                with perf.span("m4-draft"):
                    _call("claude-opus-5", "anthropic", 1_000, 2_000)
                    _call("claude-opus-5", "anthropic", 500, 500)
                with perf.span("m5-image"):
                    _call("gpt-image-2", "openai", 300, 100)
                trace.finish()
        finally:
            perf.current_trace.set(None)

        summary = [r.getMessage() for r in caplog.records if "토큰 요약" in r.getMessage()]
        # 헤더 + 단계 2줄 + 합계
        assert len(summary) == 4
        draft_line = next(line for line in summary if "m4-draft" in line and "회" in line)
        assert "2회" in draft_line
        assert "입력=1,500" in draft_line
        assert "출력=2,500" in draft_line
        image_line = next(line for line in summary if "m5-image" in line)
        assert "단가 미등록" in image_line
        total_line = next(line for line in summary if "합계" in line)
        assert "입력=1,800" in total_line
        assert "(단가 미등록 모델 제외)" in total_line
