"""파이프라인 단계별 성능 계측.

측정 없이 "어디가 느리다"를 추측하지 않기 위한 장치다. 파이프라인 실행(M3 검증,
M4+M5 생성) 하나가 trace 하나이고, 그 안의 구간(span)마다 시작 오프셋과 소요를
monotonic clock으로 남긴다. 시작 오프셋을 함께 남기는 이유: 소요의 합(busy)과
사용자가 실제로 기다린 시간(wall)은 병렬 구간이 있으면 다르다 — 오프셋이 있어야
로그만으로 어떤 구간이 겹쳐 돌았는지(critical path) 재구성할 수 있다.

로그에는 크기·개수·소요·상태만 남긴다. 사용자 입력 원문, API 키, 원고 본문,
계정 정보는 싣지 않는다.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.shared.token_costs import estimate_cost_usd

logger = logging.getLogger("app.perf")


# 토큰 로그에 쓰는 단계 이름의 한글 표기(2026-08-11 사용자 요청). span 이름 자체는
# 코드 식별자로 남긴다 — PERF span 로그·테스트·과거 로그 검색이 그 이름을 쓴다.
_STAGE_LABELS: dict[str, str] = {
    "provider_call": "단계 미지정",
    # M3 검증
    "web_research": "웹 자료 수집",
    "naver_blog_research": "네이버 블로그 보강 수집",
    "verification_llm": "자료 정리·의도 후보",
    # M4 준비(계획)
    "reference_evidence": "자료 근거 분석",
    "reference_evidence_llm": "자료 근거 분석",
    "reference_evidence_prefetch": "자료 근거 분석(사전)",
    "title_plan": "제목 방향 설계",
    "title_plan_llm_attempt": "제목 방향 설계",
    "title_plan_prefetch": "제목 방향 설계(사전)",
    "editorial_style": "편집 문체 설계",
    "editorial_style_llm": "편집 문체 설계",
    "editorial_style_prefetch": "편집 문체 설계(사전)",
    "generation_plans_prefetch": "생성 계획 사전 준비",
    "seo_keyword_plan_llm": "SEO 키워드 배치",
    "content_plan_llm": "원고 구조 설계",
    # M4 본문·마무리
    "draft_llm_attempt": "본문 작성",
    "draft_quality_check": "품질 검사",
    "content_validation": "콘텐츠 검증",
    "final_review": "최종 검수",
    "dual_critique": "이중 비평",
    "critique_integration": "비평 통합·재작성",
    "polish": "문장 다듬기",
    # M4 이미지 계획·M5 이미지
    "visual_plan_llm": "이미지 구성 계획",
    "web_photo_gate": "웹 사진 적합성 판정",
    "image_generation_total": "이미지 생성",
}
_NUMBERED_STAGE = re.compile(r"^(.+?)_(\d+)$")


def _stage_label(stage: str) -> str:
    """span 이름을 한글 표기로. 번호 붙은 이름(draft_llm_attempt_2 등)은 'N차'로 푼다.
    모르는 이름은 그대로 보여 준다 — 빈칸보다 원어가 낫다."""
    if stage in _STAGE_LABELS:
        return _STAGE_LABELS[stage]
    numbered = _NUMBERED_STAGE.match(stage)
    if numbered and numbered.group(1) in _STAGE_LABELS:
        return f"{_STAGE_LABELS[numbered.group(1)]} {numbered.group(2)}차"
    return stage


def _fmt_tokens(count: int | None) -> str:
    return f"{count:,}" if isinstance(count, int) else "-"


def _fmt_cost(cost: float | None) -> str:
    """비용 표시. 단가 미등록 모델은 0으로 속이지 않고 그렇다고 말한다."""
    return f"${cost:.4f}" if cost is not None else "단가 미등록"


@dataclass
class Span:
    stage: str
    start_offset: float  # trace 시작 기준 초
    duration: float  # 초
    ok: bool
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PerfTrace:
    """한 번의 파이프라인 실행. post_id와 별개의 trace_id를 두는 이유: 같은 글에서
    '다시 검증'·'다시 생성'이 여러 번 돌 수 있고, 각 실행을 구분해야 재시도가
    누적 시간에 어떻게 기여했는지 보인다."""

    pipeline: str  # "m3-verify" | "m4-draft" 등
    post_id: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    origin: float = field(default_factory=time.monotonic)
    spans: list[Span] = field(default_factory=list)
    # (단계, provider, 모델) -> 누적 토큰·비용. finish()의 토큰 요약이 읽는다.
    provider_usage: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )

    def record_provider_usage(
        self,
        stage: str,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost: float | None,
    ) -> None:
        """provider 호출 1건의 토큰·비용을 (단계, AI, 모델) 단위로 누적한다."""
        entry = self.provider_usage.setdefault(
            (stage, provider, model),
            {"calls": 0, "input": 0, "output": 0, "cost": 0.0, "unpriced": False},
        )
        entry["calls"] += 1
        entry["input"] += input_tokens or 0
        entry["output"] += output_tokens or 0
        if cost is None:
            # 단가를 모르는 모델. 합계에 0으로 섞으면 "전체 비용"이 실제보다 작아
            # 보이므로, 요약에서 '단가 미등록 제외'라고 명시하기 위해 표시해 둔다.
            entry["unpriced"] = True
        else:
            entry["cost"] += cost

    def record(
        self, stage: str, start: float, end: float, ok: bool = True, **meta: Any
    ) -> None:
        span = Span(
            stage=stage,
            start_offset=round(start - self.origin, 3),
            duration=round(end - start, 3),
            ok=ok,
            meta={k: v for k, v in meta.items() if v is not None},
        )
        self.spans.append(span)
        extra = " ".join(f"{k}={v}" for k, v in span.meta.items())
        logger.info(
            "PERF span | trace=%s post=%s stage=%s at=%.3fs dur=%.3fs ok=%s %s",
            self.trace_id,
            self.post_id,
            span.stage,
            span.start_offset,
            span.duration,
            span.ok,
            extra,
        )

    @contextmanager
    def span(self, stage: str, **meta: Any) -> Iterator[dict[str, Any]]:
        """구간 측정. `with trace.span("draft_llm_attempt_1") as m:` 안에서
        m["retries"]=2 처럼 메타를 덧붙일 수 있다. 예외가 나가도 구간은 기록된다
        (ok=False) — 실패한 시도가 쓴 시간도 병목의 일부다."""
        start = time.monotonic()
        collected: dict[str, Any] = dict(meta)
        token = current_stage.set(stage)
        try:
            yield collected
        except BaseException:
            self.record(stage, start, time.monotonic(), ok=False, **collected)
            raise
        else:
            self.record(stage, start, time.monotonic(), ok=True, **collected)
        finally:
            current_stage.reset(token)

    def wall_seconds(self) -> float:
        if not self.spans:
            return 0.0
        return max(s.start_offset + s.duration for s in self.spans)

    def busy_seconds(self) -> float:
        return sum(s.duration for s in self.spans if not s.meta.get("nested"))

    def finish(self) -> None:
        """요약 한 줄. wall(사용자가 기다린 시간)과 busy(구간 소요 합)를 나란히 남겨,
        둘의 차이로 병렬 구간이 실제로 겹쳐 돌았는지 확인한다."""
        stages = ", ".join(
            f"{s.stage}={s.duration:.1f}s" for s in self.spans if not s.meta.get("nested")
        )
        logger.info(
            "PERF summary | trace=%s post=%s pipeline=%s wall=%.1fs busy=%.1fs spans=%d [%s]",
            self.trace_id,
            self.post_id,
            self.pipeline,
            self.wall_seconds(),
            self.busy_seconds(),
            len(self.spans),
            stages,
        )
        self._log_token_summary()

    def _log_token_summary(self) -> None:
        """파이프라인 한 번이 단계·AI별로 쓴 토큰과 비용을 표로 남긴다.

        호출 단위 로그(record_provider_call)는 실행 중에 흩어져 지나가므로, 끝난 뒤
        "이번 실행이 결국 얼마였는지"를 한 자리에서 볼 수 있게 모아 준다.
        """
        if not self.provider_usage:
            return
        logger.info(
            "토큰 요약 | trace=%s post=%s pipeline=%s (단계 | AI/모델 | 호출 | 입력 | 출력 | 비용)",
            self.trace_id,
            self.post_id,
            self.pipeline,
        )
        total_input = total_output = 0
        total_cost = 0.0
        any_unpriced = False
        for (stage, provider, model), entry in self.provider_usage.items():
            total_input += entry["input"]
            total_output += entry["output"]
            total_cost += entry["cost"]
            any_unpriced = any_unpriced or entry["unpriced"]
            logger.info(
                "토큰 요약 | %s | %s/%s | %d회 | 입력=%s | 출력=%s | 비용=%s",
                _stage_label(stage),
                provider,
                model,
                entry["calls"],
                _fmt_tokens(entry["input"]),
                _fmt_tokens(entry["output"]),
                "단가 미등록" if entry["unpriced"] else f"${entry['cost']:.4f}",
            )
        logger.info(
            "토큰 요약 | 합계 | 입력=%s | 출력=%s | 비용=$%.4f%s",
            _fmt_tokens(total_input),
            _fmt_tokens(total_output),
            total_cost,
            " (단가 미등록 모델 제외)" if any_unpriced else "",
        )


# 실행 중인 trace·stage. LLM 호출 계층(_post_json)이 자신을 부른 단계 이름과 trace를
# 몰라도 provider 호출 메타(시도 횟수·상태·크기·토큰)를 올바른 구간에 붙일 수 있게 한다.
current_trace: contextvars.ContextVar[PerfTrace | None] = contextvars.ContextVar(
    "perf_trace", default=None
)
current_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "perf_stage", default=None
)


def start_trace(pipeline: str, post_id: str) -> PerfTrace:
    trace = PerfTrace(pipeline=pipeline, post_id=post_id)
    current_trace.set(trace)
    return trace


@contextmanager
def span(stage: str, **meta: Any) -> Iterator[dict[str, Any]]:
    """실행 중인 trace의 구간 측정. trace가 없으면(단독 호출·테스트) 측정 없이
    stage 이름만 세워 provider 호출 기록이 단계 이름을 갖게 한다."""
    trace = current_trace.get()
    if trace is not None:
        with trace.span(stage, **meta) as collected:
            yield collected
        return
    token = current_stage.set(stage)
    try:
        yield {}
    finally:
        current_stage.reset(token)


def record_provider_call(
    *,
    provider: str,
    model: str,
    start: float,
    end: float,
    status: int | str,
    attempts: int,
    response_bytes: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    ok: bool = True,
) -> None:
    """외부 provider 호출 1건의 기록. 성능 span은 trace가 있을 때만 남지만,
    토큰·비용 로그는 trace 없이도(예: trace를 시작하지 않는 M2 경로) 터미널에 찍는다.

    nested=True로 표시해 상위 단계 span(예: draft_llm_attempt_1)과 이중으로 합산되지
    않게 한다 — busy 합계는 상위 span만 센다.
    """
    trace = current_trace.get()
    stage = current_stage.get() or "provider_call"
    # usage를 보고한 호출만 토큰 줄을 남긴다. usage가 없는 호출(일부 오류 응답 등)은
    # 표시할 토큰이 없고, 매 호출 "-"만 찍으면 터미널만 시끄럽다.
    if input_tokens is not None or output_tokens is not None:
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        logger.info(
            "토큰 사용 | 단계=%s | AI=%s/%s | 입력=%s | 출력=%s | 비용=%s",
            _stage_label(stage),
            provider,
            model,
            _fmt_tokens(input_tokens),
            _fmt_tokens(output_tokens),
            _fmt_cost(cost),
        )
        if trace is not None:
            trace.record_provider_usage(
                stage, provider, model, input_tokens, output_tokens, cost
            )
    if trace is None:
        return
    trace.record(
        f"{stage}:call",
        start,
        end,
        ok=ok,
        nested=True,
        provider=provider,
        model=model,
        status=status,
        attempts=attempts,
        response_bytes=response_bytes,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
