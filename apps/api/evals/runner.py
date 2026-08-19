"""기준선 측정 실행기.

운용 코드를 흉내내지 않고 **그대로 부른다** — 프롬프트·어댑터·파서·품질검사는 전부 앱의 것을
쓰고, 이 파일은 호출 순서와 지표 수집만 담당한다. 순서는 `draft/service.py`의
`_run_draft_generation_locked`(근거 → 제목 → 문체 → 설계·SEO 병렬 → 원고 → 사진계획)를
따른다. DB·Redis·HTTP 서버는 쓰지 않는다.

모드
  fixture     : provider 응답을 가짜로 돌려준다. 비용 0. 배선·지표 확인용이며 기준선이 아니다.
  live-titles : M2 제목 생성 + 제목 평가만 실제 호출.
  live-full   : M2 + M4 7단계까지 실제 호출. 비싸고 느리다.

모든 모드에서 provider 호출을 가로채 stop_reason·토큰·지연을 기록한다. 지금 운용 코드는
stop_reason을 보지 않으므로(1-1 조사), **잘림이 이미 일어나고 있는지**는 여기서만 알 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.llm import live_adapters
from app.llm.live_adapters import (
    AnthropicDraftGenerator,
    AnthropicTopicEvaluator,
    AnthropicTopicGenerator,
)
from app.llm.prompts import article_length_targets
from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
from app.modules.draft.card_selection import MIN_NECESSITY_SCORE  # noqa: F401  (문서용)
from app.modules.draft.content_validation import run_content_validations
from app.modules.draft.quality import check_draft
from app.modules.trend.topic_scoring import (
    TitleJudgmentScore,
    build_context,
    score_titles,
)
from app.shared import DraftFormat, DraftGenerationInput, ReferenceMaterialType

from . import fixtures
from .cases import EvalCase, all_cases
from .metrics import (
    DraftMetrics,
    TitleMetrics,
    draft_metrics,
    rotation_determinism,
    title_metrics,
    visual_plan_metrics,
)

M4_PROMPT_VERSION = "m4-draft@v2.0"

# 도구 이름 → 어느 단계의 호출인지. 요청 본문만 보고 단계를 알 수 있어서 별도 상태가 필요 없다.
STAGE_BY_TOOL = {
    "return_title_candidates": "M2 제목 생성",
    "return_title_scores": "M2 제목 평가",
    "return_keyword_relevance": "M2 관련도 채점",
    "return_title_plan": "M4 제목 계획",
    "return_reference_evidence": "M4 참고 근거",
    "return_editorial_style_plan": "M4 편집 문체",
    "return_content_plan": "M4 콘텐츠 설계",
    "return_seo_keyword_plan": "M4 SEO 계획",
    "return_blog_draft": "M4 본문",
    "return_card_plan": "M4 카드 계획",
}


@dataclass
class CallRecord:
    stage: str
    model: str
    max_tokens: int | None
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    truncated: bool
    error: str | None = None


class CallRecorder:
    """`live_adapters._post_json`을 감싸 요청·응답의 사실만 기록한다.

    운용 코드를 고치지 않는다 — 모듈 전역 이름을 잠시 바꿔 끼우고 끝나면 되돌린다. 기준선
    단계에서는 코드를 수정하지 않기로 했으므로(PDF 2-5) 이 방식이어야 한다.
    """

    def __init__(self) -> None:
        self.records: list[CallRecord] = []
        self._original: Any = None

    def __enter__(self) -> CallRecorder:
        self._original = live_adapters._post_json
        original = self._original
        records = self.records

        async def recording(url: str, headers: dict, body: Any, attempts: int | None = None):
            tool = ""
            if isinstance(body, dict):
                tools = body.get("tools") or []
                if tools and isinstance(tools[0], dict):
                    tool = str(tools[0].get("name", ""))
            stage = STAGE_BY_TOOL.get(tool, f"기타({tool or url})")
            started = time.perf_counter()
            try:
                payload = (
                    await original(url, headers, body)
                    if attempts is None
                    else await original(url, headers, body, attempts=attempts)
                )
            except Exception as error:  # 실패도 기록해야 기준선이 정직하다
                records.append(
                    CallRecord(
                        stage=stage,
                        model=str((body or {}).get("model", "")) if isinstance(body, dict) else "",
                        max_tokens=(body or {}).get("max_tokens") if isinstance(body, dict) else None,
                        stop_reason=None,
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        truncated=False,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                raise
            usage = (payload or {}).get("usage") or {} if isinstance(payload, dict) else {}
            stop_reason = (payload or {}).get("stop_reason") if isinstance(payload, dict) else None
            records.append(
                CallRecord(
                    stage=stage,
                    model=str((body or {}).get("model", "")) if isinstance(body, dict) else "",
                    max_tokens=(body or {}).get("max_tokens") if isinstance(body, dict) else None,
                    stop_reason=stop_reason,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    truncated=stop_reason == "max_tokens",
                )
            )
            return payload

        live_adapters._post_json = recording
        return self

    def __exit__(self, *exc) -> None:
        live_adapters._post_json = self._original


class FixtureTransport:
    """provider를 부르지 않고 미리 만든 응답을 돌려준다."""

    def __init__(self) -> None:
        self._original: Any = None
        self.unknown_tools: list[str] = []

    def __enter__(self) -> FixtureTransport:
        self._original = live_adapters._post_json
        table = fixtures.responses()
        unknown = self.unknown_tools

        async def stub(url: str, headers: dict, body: Any, attempts: int | None = None):
            tools = (body or {}).get("tools") or []
            tool = str(tools[0].get("name", "")) if tools else ""
            value = table.get(tool)
            if value is None:
                unknown.append(tool)
                value = {}
            await asyncio.sleep(0)
            return fixtures.tool_payload(tool, value)

        live_adapters._post_json = stub
        return self

    def __exit__(self, *exc) -> None:
        live_adapters._post_json = self._original


@dataclass
class CaseResult:
    case_id: str
    topic_kind: str
    topic: str
    purpose: str
    persona: str
    is_conflict: bool
    tags: list[str]
    note: str
    titles: dict | None = None
    # '제목 추천 다시'를 누른 두 번째 배치. live-regen 모드에서만 채워진다.
    regenerated: dict | None = None
    draft: dict | None = None
    visual: dict | None = None
    stages_parsed: dict[str, bool] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _role(role: LlmRole, model: str, api_key: str) -> RoleConfig:
    return RoleConfig(
        role=role,
        label=role.value,
        provider=LlmProvider.ANTHROPIC,
        model=model,
        api_key_env="ANTHROPIC_API_KEY",
        api_key=api_key,
        has_credentials=True,
    )


def _configured_anthropic_model() -> str:
    """앱이 M4 원고에 쓰도록 설정된 모델. 환경변수 override까지 반영한다."""
    from app.llm.provider_config import ROLE_SPECS
    import os

    spec = next(spec for spec in ROLE_SPECS if spec.role is LlmRole.M4_DRAFT)
    return os.environ.get(spec.model_env) or spec.default_model


def _reference_text(case: EvalCase) -> str:
    parts = [
        material.value
        for material in case.topic.reference_materials
        if material.type is ReferenceMaterialType.TEXT
    ]
    parts += [source.snippet for source in case.topic.sources]
    return "\n".join(parts)


async def _run_titles(
    case: EvalCase,
    role: RoleConfig,
    *,
    exclude_titles: list[str] | None = None,
    previous_combos: set[tuple[str, str]] | None = None,
) -> tuple[TitleMetrics, list]:
    from app.llm.contracts import TitleEvaluationInput, TopicGenerationInput

    generator = AnthropicTopicGenerator(role)
    keyword = case.trend_keyword_model()
    result = await generator.generate_topics(
        TopicGenerationInput(
            post_id=f"post_{case.case_id}",
            input=case.blog_input,
            trend_keyword=keyword,
            settings=case.settings,
            exclude_titles=list(exclude_titles or []),
        )
    )
    candidates = list(result.topic_candidates)

    judgments: dict[str, TitleJudgmentScore] | None = None
    try:
        evaluator = AnthropicTopicEvaluator(role)
        raw = await evaluator.evaluate_titles(
            TitleEvaluationInput(
                input=case.blog_input,
                trend_keyword=keyword,
                titles=[candidate.title for candidate in candidates],
            )
        )
        judgments = {
            title: TitleJudgmentScore(
                relevance=judgment.relevance,
                trend_reflection=judgment.trend_reflection,
                purpose_match=judgment.purpose_match,
                audience_interest=judgment.audience_interest,
                reason=judgment.reason,
            )
            for title, judgment in raw.items()
        }
    except Exception:
        judgments = None

    context = build_context(
        topic=case.topic.topic,
        subject=case.topic.subject,
        purpose=[case.purpose],
        audience=case.topic.target_reader,
        trend_keyword=keyword.keyword,
    )
    scored = score_titles(candidates, context, judgments)

    return (
        title_metrics(
            scored,
            exclude_titles=list(exclude_titles or []),
            has_reference_material=bool(case.topic.reference_materials),
            previous_combos=previous_combos,
        ),
        scored,
    )


def _combos(candidates: list) -> set[tuple[str, str]]:
    """(hookType, titleType) 조합 집합. 재생성이 관점을 정말로 바꿨는지 보는 데 쓴다."""
    return {
        (
            candidate.hook_type.value if candidate.hook_type else "",
            candidate.description or "",
        )
        for candidate in candidates
    }


async def _run_draft(case: EvalCase, role: RoleConfig) -> tuple[DraftMetrics, dict, dict]:
    generator = AnthropicDraftGenerator(role)
    draft_input = DraftGenerationInput(
        post_id=f"post_{case.case_id}",
        user_id="eval",
        input=case.blog_input,
        selected_intent=case.selected_intent,
        prompt_version=M4_PROMPT_VERSION,
        settings=case.settings,
        format=DraftFormat.MARKDOWN,
        trend_title=case.trend_title,
    )
    parsed: dict[str, bool] = {}

    evidence = await _safe(generator.generate_reference_evidence(draft_input))
    parsed["M4 참고 근거"] = evidence is not None
    if evidence is not None:
        draft_input = draft_input.model_copy(update={"reference_evidence": evidence})

    title_plan = await _safe(generator.generate_title_plan(draft_input))
    parsed["M4 제목 계획"] = title_plan is not None
    if title_plan is not None:
        draft_input = draft_input.model_copy(update={"title_plan": title_plan})

    style = await _safe(generator.generate_editorial_style_plan(draft_input))
    parsed["M4 편집 문체"] = style is not None
    if style is not None:
        draft_input = draft_input.model_copy(update={"editorial_style": style})

    content_plan, seo_plan = await asyncio.gather(
        _safe(generator.generate_content_plan(draft_input)),
        _safe(generator.generate_seo_keyword_plan(draft_input)),
    )
    parsed["M4 콘텐츠 설계"] = content_plan is not None
    parsed["M4 SEO 계획"] = seo_plan is not None
    draft_input = draft_input.model_copy(
        update={"content_plan": content_plan, "seo_keyword_plan": seo_plan}
    )

    result = await generator.generate_draft(draft_input)
    parsed["M4 본문"] = result is not None
    post = result.final_post

    # 렌더링 시각자료 수·참고 이미지 수는 사진 예산 계산에 들어간다. 운용에서는 설계 결과와
    # 업로드 자료에서 나오므로, 여기서도 같은 자리에서 센다.
    rendered_count = sum(
        1
        for section in list(getattr(content_plan, "sections", []) or [])
        if str(getattr(getattr(section, "visual_type", None), "value", "NONE")) != "NONE"
    )
    reference_images = sum(
        1
        for material in case.topic.reference_materials
        if material.type is ReferenceMaterialType.IMAGE
    )
    card_plan = await _safe(
        generator.generate_visual_card_plan(
            draft_input, post, rendered_count, reference_images
        )
    )
    parsed["M4 카드 계획"] = card_plan is not None

    target_min, target_max = article_length_targets(case.settings)
    report = check_draft(
        post,
        hashtag_count=case.settings.hashtag_count,
        min_body_chars=target_min,
        max_body_chars=target_max,
        trend_title=case.trend_title,
        trend_keyword=case.topic.trend_keyword,
        has_experience_material=case.has_experience_material,
        photo_count=0,
        final_title=title_plan.primary_title if title_plan else None,
    )
    validation = None
    try:
        validation = run_content_validations(
            post,
            seo_plan,
            case.topic.sources,
            case.topic.reference_materials,
            purposes=[case.purpose],
            evidence=evidence,
            title_locked=bool(case.trend_title or title_plan),
        )
    except Exception:
        validation = None

    draft = draft_metrics(
        post,
        target_min=target_min,
        target_max=target_max,
        seo_plan=seo_plan,
        reference_text=_reference_text(case),
        quality_report=report,
        validation_result=validation,
        revision_attempts=0,
    )
    budget = 0
    if style is not None:
        budget = int(getattr(getattr(style, "visual_budget", None), "rendered_visuals_max", 0) or 0)
    visual = visual_plan_metrics(
        card_plan=card_plan,
        content_plan=content_plan,
        rendered_budget=budget,
        has_numeric_material=any(
            material.type is ReferenceMaterialType.TEXT
            and any(char.isdigit() for char in material.value)
            for material in case.topic.reference_materials
        ),
        post_id=f"post_{case.case_id}",
    )
    return draft, visual.as_dict(), parsed


async def _safe(awaitable):
    try:
        return await awaitable
    except Exception:
        return None


# 사례 하나(최대 8호출)의 상한. 한 호출이 매달려도 측정 전체가 멈추지 않게 한다.
# 공용 httpx 클라이언트의 read 타임아웃이 300초라, 재시도까지 겹치면 한 호출이 20분을
# 붙잡을 수 있다 — 실제로 그렇게 한 번 멈춰서 이 상한을 넣었다.
CASE_TIMEOUT_SECONDS = 600.0


async def run(
    *,
    mode: str = "fixture",
    limit: int | None = None,
    model: str | None = None,
    api_key: str = "eval-fixture-key",
    only_tags: set[str] | None = None,
    only_ids: list[str] | None = None,
    progress_path: Path | None = None,
) -> dict:
    cases = all_cases()
    if only_tags:
        cases = [case for case in cases if only_tags & set(case.tags)]
    if only_ids:
        cases = [
            case for case in cases if any(needle in case.case_id for needle in only_ids)
        ]
    if limit is not None:
        cases = cases[:limit]

    # 기본값을 하드코딩하지 않는다 — 기준선은 실제로 배포된 모델로 재야 의미가 있다.
    # (전환 전 측정치는 evals/baseline-live-*.json에 그 시점 모델명과 함께 남아 있다.)
    resolved_model = model or _configured_anthropic_model()
    topic_role = _role(LlmRole.M2_TOPIC, resolved_model, api_key)
    draft_role = _role(LlmRole.M4_DRAFT, resolved_model, api_key)

    results: list[CaseResult] = []
    transport = FixtureTransport() if mode == "fixture" else None

    def _enter():
        return transport.__enter__() if transport else None

    _enter()
    try:
        for case in cases:
            result = CaseResult(
                case_id=case.case_id,
                topic_kind=case.topic.kind,
                topic=case.topic.topic,
                purpose=case.purpose,
                persona=case.persona_label,
                is_conflict=case.is_conflict,
                tags=list(case.tags),
                note=case.note,
            )
            with CallRecorder() as recorder:
                try:
                    titles, scored = await asyncio.wait_for(
                        _run_titles(case, topic_role), timeout=CASE_TIMEOUT_SECONDS
                    )
                    result.titles = titles.as_dict()
                    if mode == "live-regen":
                        # 같은 소재로 '제목 추천 다시'를 누른 상황. 화면에 있던 제목을
                        # exclude_titles로 넘기는 것이 운용 동작이다(StepTrends.tsx:761).
                        again, _ = await _run_titles(
                            case,
                            topic_role,
                            exclude_titles=[c.title for c in scored],
                            previous_combos=_combos(scored),
                        )
                        result.regenerated = again.as_dict()
                    if mode in {"fixture", "live-full"}:
                        draft, visual, parsed = await asyncio.wait_for(
                            _run_draft(case, draft_role), timeout=CASE_TIMEOUT_SECONDS
                        )
                        result.draft = draft.as_dict()
                        result.visual = visual
                        result.stages_parsed = parsed
                except Exception as error:
                    result.error = f"{type(error).__name__}: {error}"
                result.calls = [asdict(record) for record in recorder.records]
            results.append(result)
            # 사례마다 즉시 append한다. 마지막에 한 번만 저장하면 중간에 끊겼을 때 몇 시간의
            # 실제 호출이 통째로 사라진다(실제로 한 번 그랬다).
            if progress_path is not None:
                with progress_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(result.as_dict(), ensure_ascii=False) + "\n")
    finally:
        if transport:
            transport.__exit__()

    return {
        "mode": mode,
        "model": resolved_model,
        "case_count": len(results),
        "rotation": rotation_determinism(),
        "cases": [result.as_dict() for result in results],
        "unknown_fixture_tools": sorted(set(transport.unknown_tools)) if transport else [],
    }
