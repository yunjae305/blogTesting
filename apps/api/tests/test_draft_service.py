"""M4 원고 생성과 사용자 수정 저장."""

import asyncio
import base64
import io
import logging

import pytest
from PIL import Image

import app.modules.draft.service as draft_service_module
from app.errors import BlogTaskError
from app.llm.parsing import seo_keyword_plan_from_json
from app.llm.parsing import ProviderTruncatedError
from app.llm.prompts import draft_prompt
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.draft.service import DraftService
from app.modules.draft.reference_evidence import build_profile
from app.modules.persona import InMemoryPersonaRepository, PersonaService
from app.modules.user_settings.repository import InMemoryUserSettingsRepository
from app.modules.user_settings.service import UserSettingsService
from app.modules.user_settings.validation import UpsertUserSettingsInput
from app.shared import (
    PHASE_STEPS,
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    DraftGenerationResult,
    DraftFormat,
    FinalPost,
    FinalReviewIssue,
    GeneratedPostImage,
    PolishEdit,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SelectedIntent,
    TaskPhase,
    TaskProgress,
    TitlePlan,
    TrendSelection,
)

NOW = "1970-01-01T00:00:00.000Z"


def build_persona_service() -> PersonaService:
    return PersonaService(InMemoryPersonaRepository())


def build_user_settings_service() -> UserSettingsService:
    return UserSettingsService(InMemoryUserSettingsRepository(), build_persona_service())

# 진짜 M4가 내놓는 크기여야 한다. "Generated body"(14자)로는 품질 검사를 통과할 수 없고,
# 통과해서도 안 된다 — 그런 원고가 사용자 화면에 올라가면 그게 버그다.
GENERATED_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    # 24문단 ≈ 1990자 — 기본(중간) 목표 1800~2300자 안이고, 짧게의 통과 상한(1,200×1.3=1,560)은
    # 넘어서 '짧게 설정 + 긴 원고' 재작성 테스트의 입력으로도 쓰인다.
    for n in range(1, 25)
)

DRAFT_RESULT = DraftGenerationResult(
    prompt_version="m4-draft@v1.0",
    provider="stub",
    model="stub-draft-generator",
    generated_at=NOW,
    final_post=FinalPost(
        title="Generated title",
        body=GENERATED_BODY,
        hashtags=["AI", "Blog"],
        thumbnail_copy=["대표 문구", "두 줄까지"],
        html_content=f"<article><h1>Generated title</h1><p>{GENERATED_BODY}</p></article>",
        markdown_content=f"# Generated title\n\n{GENERATED_BODY}",
    ),
)

# What M4 actually returns now: two body slots marked for an image, and the copy the
# cover gets lettered with.
TAGGED_RESULT = DRAFT_RESULT.model_copy(
    update={
        "final_post": DRAFT_RESULT.final_post.model_copy(
            update={
                "body": f"{GENERATED_BODY} [[IMAGE: one]] B [[IMAGE: two]] C",
                "html_content": f"<p>{GENERATED_BODY}</p><p>[[IMAGE: one]]</p><p>B</p><p>[[IMAGE: two]]</p><p>C</p>",
                "markdown_content": f"{GENERATED_BODY}\n\n[[IMAGE: one]]\n\nB\n\n[[IMAGE: two]]\n\nC",
            }
        )
    }
)


class StubDraftGenerator:
    def __init__(self, result: DraftGenerationResult = DRAFT_RESULT):
        self.result = result
        self.captured: list = []

    async def generate_draft(self, draft_input):
        self.captured.append(draft_input)
        return self.result

    async def generate_reference_evidence(self, draft_input):
        profile = build_profile(draft_input.input.reference_materials)
        return profile.model_copy(
            update={
                "reference_image_roles": [
                    role.model_copy(update={"privacy_scanned": True})
                    for role in profile.reference_image_roles
                ]
            }
        )


def valid_image_data_url(format_name: str, color: tuple[int, int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format=format_name)
    mime = "image/jpeg" if format_name == "JPEG" else "image/png"
    return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


class SequenceDraftGenerator:
    """호출할 때마다 다른 결과를 준다 — 재생성을 보려면 두 번째 답이 달라야 한다."""

    def __init__(self, results: list[DraftGenerationResult]):
        self.results = results
        self.calls = 0
        self.captured: list = []

    async def generate_draft(self, draft_input):
        self.captured.append(draft_input)
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


# 최소 분량에 못 미치는 원고. 첫 시도에서 이걸 내주면 품질 검사가 반려하고 재생성이 돈다.
SHORT_RESULT = DRAFT_RESULT.model_copy(
    update={
        "final_post": DRAFT_RESULT.final_post.model_copy(
            update={
                "body": "짧은 원고입니다. " * 20,
                "html_content": "<article><h1>Generated title</h1><p>짧은 원고입니다.</p></article>",
                "markdown_content": "# Generated title\n\n짧은 원고입니다.",
            }
        )
    }
)

SHORT_FIT_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    # 12문단 ≈ 994자 — 짧게 목표 800~1200자 안.
    for n in range(1, 13)
)

SHORT_FIT_RESULT = DRAFT_RESULT.model_copy(
    update={
        "final_post": DRAFT_RESULT.final_post.model_copy(
            update={
                "body": SHORT_FIT_BODY,
                "html_content": (
                    f"<article><h1>Generated title</h1><h2>첫 소제목</h2>"
                    f"<p>{SHORT_FIT_BODY}</p><h2>둘째 소제목</h2><p>마무리입니다.</p></article>"
                ),
                "markdown_content": f"# Generated title\n\n{SHORT_FIT_BODY}",
            }
        )
    }
)

MILD_OVER_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    # 16문단 ≈ 1326자 — 짧게 목표 상한(1200)은 넘지만 통과 상한(1,560)은 안 넘어,
    # 경고도 재작성도 없이 그대로 수용돼야 한다.
    for n in range(1, 17)
)
MILD_OVER_RESULT = DRAFT_RESULT.model_copy(
    update={
        "final_post": DRAFT_RESULT.final_post.model_copy(
            update={
                "body": MILD_OVER_BODY,
                "html_content": (
                    f"<article><h1>Generated title</h1><p>{MILD_OVER_BODY}</p></article>"
                ),
                "markdown_content": f"# Generated title\n\n{MILD_OVER_BODY}",
            }
        )
    }
)


class StubImageGenerator:
    def __init__(self):
        self.calls: list = []

    async def generate_post_image(self, image_input):
        self.calls.append(image_input)
        return GeneratedPostImage(
            data_url=f"data:image/jpeg;base64,abc12{image_input.image_index}",
            alt_text=f"Generated image alt {image_input.image_index + 1}",
            prompt=image_input.content_prompt or f"image prompt {image_input.image_index + 1}",
            provider="openai",
            model="gpt-image-2",
            generated_at=NOW,
            mime_type="image/jpeg",
            source="generated",
        )


class SlowImageGenerator(StubImageGenerator):
    """Records how many image calls were in flight at once.

    Asserting on the images that come back cannot tell a parallel run from a
    sequential one — only the overlap can.
    """

    def __init__(self, delay: float = 0.05):
        super().__init__()
        self._delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    async def generate_post_image(self, image_input):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            return await super().generate_post_image(image_input)
        finally:
            self.in_flight -= 1


def build_task(**overrides) -> BlogTask:
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        status=BlogTaskStatus.INTENT_SELECTED,
        version=5,
        created_at=NOW,
        updated_at=NOW,
        status_history=[],
        input=BlogTaskInput(
            topic="블로그 자동화", keywords=["AI", "블로그"], reference_materials=[]
        ),
        posting_logs=[],
        selected_intent=SelectedIntent(
            intent_id="intent_1",
            title="AI 블로그 실무 가이드",
            target_reader="실무자",
            rationale="실무 적용 관점",
        ),
    )
    return BlogTask(**{**defaults, **overrides})


def build_service(
    draft_generator=None,
    post_image_generator=None,
    user_settings_service=None,
    final_reviewer=None,
):
    repository = InMemoryBlogTaskRepository()
    service = DraftService(
        repository=repository,
        draft_generator=draft_generator or StubDraftGenerator(),
        post_image_generator=post_image_generator,
        user_settings_service=user_settings_service,
        persona_service=build_persona_service(),
        final_reviewer=final_reviewer,
    )
    return repository, service


async def test_legacy_secret_search_source_is_removed_at_the_m4_boundary():
    _repository, service = build_service()
    task = build_task()
    secret = "must-not-reach-m4"
    task = task.model_copy(
        update={
            "selected_intent": task.selected_intent.model_copy(
                update={
                    "sources": [
                        SearchSource(
                            title="과거 출처",
                            url=f"https://example.com/report?access_token={secret}",
                            snippet="과거 저장 자료",
                        ),
                        SearchSource(
                            title="공개 출처",
                            url="https://example.com/public",
                            snippet="공개 자료",
                        ),
                    ]
                }
            )
        }
    )

    draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)

    assert [source.url for source in draft_input.selected_intent.sources] == [
        "https://example.com/public"
    ]


async def test_generate_stores_final_post_and_advances_to_ready_to_publish():
    repository, service = build_service()
    await repository.create(build_task())

    updated = await service.generate_draft(
        "post_1", {"style": "professional", "format": "html"})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post.title == "Generated title"


async def test_중단된_채_GENERATING에_남은_글도_다시_생성할_수_있다():
    """프로세스가 죽으면 잡은 사라지는데 글은 GENERATING에 남는다.

    복구 스위퍼가 그것을 되돌리지만 **시작한 지 얼마 안 된 작업은 15분간 유예한다**
    (recovery.FRESH_SECONDS — 멀쩡히 돌던 원고를 회수하지 않으려는 장치다). 그 창
    안에서는 사용자가 할 수 있는 일이 없었다: 화면은 '아직 돌고 있어요'라 말하고,
    재시도를 누르면 `M4 requires INTENT_SELECTED, received GENERATING`으로 거절됐다.

    실제로 그렇게 막혔다(2026-08-06 신고 — 진행 기록은 13분째 멈춰 있는데 재시도가
    계속 안 됐다). 명시적인 요청은 유예보다 앞선다.
    """
    repository, service = build_service()
    await repository.create(build_task(status=BlogTaskStatus.GENERATING))

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post is not None


async def test_원고가_이미_있는_글은_GENERATING이어도_되살리지_않는다():
    """되살리기는 '아직 원고가 없다'가 전제다. 원고가 있으면 다시 쓸 이유가 없고,
    쓰면 완성된 것을 덮어쓴다."""
    repository, service = build_service()
    await repository.create(
        build_task(status=BlogTaskStatus.GENERATING, final_post=DRAFT_RESULT.final_post)
    )

    with pytest.raises(BlogTaskError) as caught:
        await service.generate_draft("post_1", {})

    assert caught.value.code == "INVALID_STATUS_TRANSITION"
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.GENERATING


async def test_다른_프로세스가_임차를_쥐고_있으면_되살리지_않는다():
    """임차를 쥔 쪽이 있으면 그 글은 **지금 정말 돌고 있다.** 되살리면 두 벌이 돈다 —
    LLM·이미지 비용이 그대로 두 배가 되고 진행 기록을 서로 덮어쓴다."""

    class HeldLease:
        async def acquire(self, key):
            return None

        async def release(self, key, token):
            return None

        async def renew(self, key, token):
            return False

        async def is_held(self, key):
            return True

    repository = InMemoryBlogTaskRepository()
    service = DraftService(
        repository=repository,
        draft_generator=StubDraftGenerator(),
        persona_service=build_persona_service(),
        job_lease=HeldLease(),
    )
    await repository.create(build_task(status=BlogTaskStatus.GENERATING))

    with pytest.raises(BlogTaskError) as caught:
        await service.generate_draft("post_1", {})

    assert caught.value.code == "DRAFT_IN_PROGRESS"
    # 상태를 건드리지 않았다 — 돌고 있는 쪽이 끝내면 그대로 이어진다.
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.GENERATING


async def test_되살릴_때_멈춰_있던_진행_표시를_지운다():
    """남겨 두면 새 실행의 1단계 위에 옛 4단계가 겹쳐, 화면이 거꾸로 가는 것처럼 보인다."""
    repository, service = build_service()
    await repository.create(build_task(status=BlogTaskStatus.GENERATING))
    await repository.update_progress(
        "post_1",
        TaskProgress(
            phase=TaskPhase.DRAFT,
            step=2,
            total_steps=4,
            label="본문 원고 작성",
            steps=list(PHASE_STEPS[TaskPhase.DRAFT]),
            started_at=NOW,
            updated_at=NOW,
        ),
    )

    await service.generate_draft("post_1", {})

    # 생성이 끝나면 진행 표시는 어차피 정리된다 — 여기서 보는 것은 **옛 4단계가 새
    # 실행으로 넘어오지 않는다**는 것이다. 새 실행이 남긴 것만 있어야 한다.
    progress = (await repository.find_by_post_id("post_1")).progress
    assert progress is None or progress.label != "본문 원고 작성"


async def test_a_short_first_draft_is_regenerated_with_the_failure_reason():
    """전체를 무턱대고 다시 생성하지 않는다: 첫 원고가 분량 미달로 반려되면, 그 사유가
    두 번째 시도 프롬프트(revision_notes)에 실려 모델이 그 문제만 고치게 한다."""
    generator = SequenceDraftGenerator([SHORT_RESULT, DRAFT_RESULT])
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert generator.calls == 2
    # 첫 시도에는 사유가 없고, 재생성에만 반려 사유가 붙는다.
    assert generator.captured[0].revision_notes is None
    assert generator.captured[0].previous_draft is None
    assert generator.captured[1].revision_notes
    assert generator.captured[1].previous_draft is not None
    assert any("최소" in note for note in generator.captured[1].revision_notes)


async def test_generate_retries_when_short_setting_is_far_over_the_maximum():
    settings_service = build_user_settings_service()
    await settings_service.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=5,
            article_length="short",
            default_persona="p_1",
            auto_posting_enabled=False,
        )
    )
    generator = SequenceDraftGenerator([DRAFT_RESULT, SHORT_FIT_RESULT])
    repository, service = build_service(
        draft_generator=generator, user_settings_service=settings_service
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post.body == SHORT_FIT_BODY
    assert generator.calls == 2
    assert generator.captured[0].revision_notes is None
    assert generator.captured[1].revision_notes
    assert generator.captured[1].previous_draft is not None
    assert any("권장 최대 1560자" in note for note in generator.captured[1].revision_notes)


async def test_a_truncated_polish_retry_keeps_the_draft_it_already_accepted(caplog):
    """길이 다듬기가 잘려도 이미 합격한 원고를 버리지 않는다(2026-08-03 실사례).

    실제로 벌어진 일: 1차 시도가 41초에 정상 원고를 냈고 품질 검사도 통과했다. 남은
    문제는 '권장 최대 1,200자를 넘겼다'는 경고 하나뿐이었다. 그 경고를 고치려는 2차
    시도가 252초 동안 최대 출력 토큰 32,000개를 전부 쓰고 잘렸고
    (ProviderTruncatedError), 그 예외가 그대로 올라가 **합격한 1차 원고까지 함께**
    버려졌다. 사용자는 7분을 기다린 뒤 '원고 생성 멈춤'을 봤다.

    다듬기는 선택이다 — 더 좋아질 기회여야지 전부 잃을 위험이어서는 안 된다.
    """
    settings_service = build_user_settings_service()
    await settings_service.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=5,
            article_length="short",
            default_persona="p_1",
            auto_posting_enabled=False,
        )
    )

    class LongThenTruncated:
        """1차는 길지만 합격, 2차(다듬기)는 잘림."""

        def __init__(self):
            self.calls = 0

        async def generate_draft(self, draft_input):
            self.calls += 1
            if self.calls == 1:
                return DRAFT_RESULT
            raise ProviderTruncatedError(
                stage="m4-draft", model="claude-opus-5", max_tokens=32000
            )

    generator = LongThenTruncated()
    repository, service = build_service(
        draft_generator=generator, user_settings_service=settings_service
    )
    await repository.create(build_task())

    with caplog.at_level(logging.WARNING):
        updated = await service.generate_draft("post_1", {})

    # 실패가 아니라 1차 원고로 완성된다.
    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post.body == DRAFT_RESULT.final_post.body
    assert generator.calls == 2
    assert any("직전 원고를 그대로 씁니다" in r.getMessage() for r in caplog.records)


async def test_a_truncation_with_nothing_to_fall_back_on_uses_the_remaining_attempt():
    """돌아갈 원고가 없으면 남은 시도를 쓴다 — 잘림은 예산 문제라 다음 번엔 통과할 수 있다."""

    class TruncatedThenFine:
        def __init__(self):
            self.calls = 0

        async def generate_draft(self, draft_input):
            self.calls += 1
            if self.calls == 1:
                raise ProviderTruncatedError(
                    stage="m4-draft", model="claude-opus-5", max_tokens=32000
                )
            return DRAFT_RESULT

    generator = TruncatedThenFine()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert generator.calls == 2


async def test_truncation_on_every_attempt_still_fails(caplog):
    """돌아갈 원고가 끝내 없으면 실패라고 말한다 — 조용히 빈 결과를 내지 않는다."""

    class AlwaysTruncated:
        def __init__(self):
            self.calls = 0

        async def generate_draft(self, draft_input):
            self.calls += 1
            raise ProviderTruncatedError(
                stage="m4-draft", model="claude-opus-5", max_tokens=32000
            )

    generator = AlwaysTruncated()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    with caplog.at_level(logging.WARNING):
        updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.FAILED
    assert generator.calls == 2
    failure = next(r for r in caplog.records if "원고 생성 실패" in r.getMessage())
    assert "ProviderTruncatedError" in failure.getMessage()


async def test_length_retry_also_fixes_a_missing_selected_trend(caplog):
    """한 번뿐인 길이 재작성에 같은 검사에서 발견한 트렌드 누락 사유도 함께 전달한다."""
    settings_service = build_user_settings_service()
    await settings_service.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=5,
            article_length="short",
            default_persona="p_1",
            auto_posting_enabled=False,
        )
    )
    trend_title = "AI 트렌드 핵심 AIONA, 지금 주목받는 배경과 변화 이유"
    corrected_body = f"{SHORT_FIT_BODY}\n\nAIONA가 주목받는 배경과 변화 이유를 짚습니다."
    corrected_result = SHORT_FIT_RESULT.model_copy(
        update={
            "final_post": SHORT_FIT_RESULT.final_post.model_copy(
                update={
                    "body": corrected_body,
                    "html_content": (
                        f"<article><h1>Generated title</h1><h2>첫 소제목</h2>"
                        f"<p>{corrected_body}</p><h2>둘째 소제목</h2><p>마무리입니다.</p></article>"
                    ),
                    "markdown_content": f"# Generated title\n\n{corrected_body}",
                }
            )
        }
    )
    generator = SequenceDraftGenerator([DRAFT_RESULT, corrected_result])
    repository, service = build_service(
        draft_generator=generator, user_settings_service=settings_service
    )
    await repository.create(
        build_task(
            trend_selection=TrendSelection(
                topic_candidate_id="topic_1",
                final_topic=trend_title,
                selected_trend_keyword_ids=["trend_1"],
                skipped=False,
                selected_at=NOW,
            )
        )
    )

    with caplog.at_level(logging.INFO):
        updated = await service.generate_draft("post_1", {})

    notes = generator.captured[1].revision_notes
    assert updated.final_post.body == corrected_body
    assert any("권장 최대 1560자" in note for note in notes)
    assert any("선택한 트렌드" in note and "AIONA" in note for note in notes)
    second_prompt = draft_prompt(generator.captured[1])
    assert "권장 최대 1560자" in second_prompt
    assert "선택한 트렌드" in second_prompt
    assert "원고 조정 재시도 (다음 시도 2/2)" in caplog.text


async def test_generate_accepts_a_mild_short_setting_overrun_without_rewriting():
    settings_service = build_user_settings_service()
    await settings_service.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=5,
            article_length="short",
            default_persona="p_1",
            auto_posting_enabled=False,
        )
    )
    generator = SequenceDraftGenerator([MILD_OVER_RESULT, SHORT_FIT_RESULT])
    repository, service = build_service(
        draft_generator=generator, user_settings_service=settings_service
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post.body == MILD_OVER_BODY
    assert generator.calls == 1


async def test_a_second_generate_is_refused_by_the_status_guard():
    """중복 생성을 막는 것은 상태 전이다 — 첫 요청이 INTENT_SELECTED를 떠나 버린다.

    멱등성 키가 하던 일이 아니라 원래부터 여기 있던 잠금이고, 키를 걷어낸 뒤에도 그대로다.
    """
    repository, service = build_service()
    await repository.create(build_task())

    await service.generate_draft("post_1", {})

    with pytest.raises(BlogTaskError) as caught:
        await service.generate_draft("post_1", {})
    assert caught.value.code == "INVALID_STATUS_TRANSITION"


async def test_a_live_run_is_not_restarted_even_after_it_marks_the_post_failed():
    """상태 전이만으로는 중복 실행을 못 막는다(2026-08-03 실사례).

    실행이 실패를 기록하면 상태는 FAILED가 되고, '다시 생성하기'는 그것을
    INTENT_SELECTED로 되돌려 재시도를 연다. 그런데 실패를 기록한 그 실행이 아직 살아
    있으면 두 벌이 같은 글을 쓰게 된다 — 실제로 한 글에 세 벌이 동시에 돌아 LLM 비용이
    세 배로 나갔고, 세 실행이 진행 상황을 번갈아 덮어써서 화면은 '1단계에서 멈춤'을
    보이는 동안 다른 실행은 3단계를 지나고 있었다.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    class MarksFailedThenKeepsRunning:
        """실패를 기록해 두고도 살아 있는 실행. 문제의 창을 그대로 만든다."""

        def __init__(self, repository):
            self._repository = repository
            self.calls = 0

        async def generate_draft(self, draft_input):
            self.calls += 1
            # 실행 중에 글이 FAILED가 된 상황(이 실행 자신이 뒤에서 실패 처리를 했거나,
            # 앞선 실행이 남긴 상태). 재시도 문이 열린 채로 이 실행은 계속 돈다.
            await self._repository.transition_status(
                "post_1", BlogTaskStatus.FAILED, "test"
            )
            started.set()
            await release.wait()
            return DRAFT_RESULT

    repository, service = build_service()
    generator = MarksFailedThenKeepsRunning(repository)
    service._draft_generator = generator

    await repository.create(build_task())
    await service.start_draft_generation("post_1", {})
    await started.wait()

    # 사용자가 '다시 생성하기'를 누른다. 상태는 FAILED라 재시도 문이 열려 있지만,
    # 첫 실행이 아직 살아 있으므로 거부돼야 한다.
    with pytest.raises(BlogTaskError) as caught:
        await service.start_draft_generation("post_1", {})
    assert caught.value.code == "INVALID_STATUS_TRANSITION"
    assert "이미 원고를 생성하고" in caught.value.message

    release.set()
    await service._jobs.drain()
    assert generator.calls == 1


async def test_a_retry_is_allowed_once_the_run_is_really_over():
    """중복 차단이 정상 재시도까지 막으면 안 된다 — 실행이 끝나면 등록이 풀린다."""
    generator = FailingDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    await service.generate_draft("post_1", {})
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.FAILED

    service._draft_generator = StubDraftGenerator()
    retried = await service.generate_draft("post_1", {})
    assert retried.status == BlogTaskStatus.READY_TO_PUBLISH


async def test_a_refused_start_does_not_leave_the_post_marked_as_running():
    """잡을 띄우기 전에 실패한 요청이 등록을 남기면 그 글은 영영 다시 시도할 수 없다."""
    repository, service = build_service()
    # 글이 없으므로 _require_intent_selected_task가 NOT_FOUND로 막는다.
    with pytest.raises(BlogTaskError):
        await service.start_draft_generation("post_1", {})
    assert "post_1" not in service._running_drafts

    await repository.create(build_task())
    updated = await service.generate_draft("post_1", {})
    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH


async def test_a_generation_failure_logs_the_exception_type_and_stack(caplog):
    """실패 로그에 예외 종류와 스택이 남아야 한다.

    예전에는 str(error)만 찍었다. 메시지가 비어 있거나 짧은 예외는 로그에 사실상
    아무것도 남기지 않아, 3분짜리 비동기 작업의 실패 원인을 추적할 수 없었다.
    """

    class BlankError(Exception):
        """메시지가 없는 예외 — str()이 빈 문자열이다."""

    class RaisesBlank:
        async def generate_draft(self, draft_input):
            raise BlankError()

    repository, service = build_service(draft_generator=RaisesBlank())
    await repository.create(build_task())

    with caplog.at_level(logging.WARNING):
        await service.generate_draft("post_1", {})

    failure = next(r for r in caplog.records if "원고 생성 실패" in r.getMessage())
    assert "BlankError" in failure.getMessage()
    assert failure.exc_info is not None


async def test_generate_passes_saved_user_settings_to_the_generator():
    settings_service = build_user_settings_service()
    await settings_service.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=9,
            default_persona="p_5",
            auto_posting_enabled=False,
        )
    )
    generator = StubDraftGenerator()
    repository, service = build_service(
        draft_generator=generator, user_settings_service=settings_service
    )
    await repository.create(build_task())

    await service.generate_draft("post_1", {})

    assert generator.captured[0].settings.hashtag_count == 9
    assert generator.captured[0].settings.article_length == "medium"
    # 문구 개정에 흔들리지 않게 페르소나 이름으로 시작하는지만 고정한다(id→프롬프트 배선 검증).
    assert generator.captured[0].settings.default_persona.startswith("실무 코치")


async def test_generate_passes_selected_intent_sources_to_the_generator():
    generator = StubDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(
        build_task(
            selected_intent=SelectedIntent(
                intent_id="intent_1",
                title="T",
                target_reader="R",
                rationale="Why",
                sources=[
                    SearchSource(title="출처", url="https://example.com", snippet="요약")
                ],
            )
        )
    )

    await service.generate_draft("post_1", {})

    assert generator.captured[0].selected_intent.sources[0].url == "https://example.com"


async def test_generate_inserts_only_the_thumbnail_when_untagged():
    images = StubImageGenerator()
    repository, service = build_service(post_image_generator=images)
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert len(updated.final_post.images) == 1
    assert updated.final_post.featured_image.model == "gpt-image-2"
    assert 'data:image/jpeg;base64,abc120' in updated.final_post.html_content
    assert "![Generated image alt 1](data:image/jpeg;base64,abc120)" in (
        updated.final_post.markdown_content
    )


async def test_images_are_generated_even_for_a_settings_doc_saved_with_the_old_image_mode():
    """AI 이미지 사용 여부 설정은 없앴다 — 항상 생성한다.

    예전 문서에는 imageMode:"off"가 남아 있을 수 있는데, 그 값이 살아 있으면 설정 화면에서
    끌 수도 없는 채로 이미지 없는 글이 계속 나온다. 저장된 값과 무관하게 생성되어야 한다.
    """
    images = StubImageGenerator()
    settings = build_user_settings_service()
    await settings.save(
        UpsertUserSettingsInput(
            user_id="user_1",
            hashtag_count=5,
            default_persona="p_1",
            auto_posting_enabled=False,
        )
    )
    # 설정 모델에 이미지 스위치가 남아 있지 않아야, 옛 문서의 값이 되살아날 길도 없다.
    stored = await settings.get_by_user_id("user_1")
    assert not hasattr(stored, "image_mode")

    repository, service = build_service(
        draft_generator=StubDraftGenerator(TAGGED_RESULT),
        post_image_generator=images,
        user_settings_service=settings,
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert images.calls
    assert updated.final_post.images
    assert "[[IMAGE:" not in updated.final_post.html_content


class FailingDraftGenerator:
    def __init__(self):
        self.calls = 0

    async def generate_draft(self, draft_input):
        self.calls += 1
        raise RuntimeError("model timed out")


async def test_start_returns_before_the_draft_is_written(caplog):
    """The point of the background job: the request comes back while M4 is still
    running, so the client is not holding a socket open for a minute."""
    caplog.set_level(logging.INFO, logger="app.modules.blog_task.jobs")
    images = SlowImageGenerator(delay=0.05)
    repository, service = build_service(post_image_generator=images)
    await repository.create(build_task())

    started = await service.start_draft_generation("post_1", {})

    assert started.status == BlogTaskStatus.GENERATING
    assert started.final_post is None

    await service._jobs.drain()

    finished = await repository.find_by_post_id("post_1")
    assert finished.status == BlogTaskStatus.READY_TO_PUBLISH
    assert finished.final_post is not None
    # Cleared once there is nothing left to report.
    assert finished.progress is None
    assert "PIPELINE summary | phase=DRAFT post=post_1 ok=true" in caplog.text


async def test_progress_reports_the_step_the_server_is_actually_on():
    seen: list[tuple[int, str]] = []

    class Watcher(SlowImageGenerator):
        async def generate_post_image(self, image_input):
            task = await repository.find_by_post_id("post_1")
            if task.progress:
                seen.append((task.progress.step, task.progress.label))
            return await super().generate_post_image(image_input)

    repository, service = build_service(post_image_generator=Watcher(delay=0.01))
    await repository.create(build_task())

    started = await service.start_draft_generation("post_1", {})
    assert started.status == BlogTaskStatus.GENERATING

    await service._jobs.drain()

    # While the images were being made, the reported step said so. 라벨은 단계 이름
    # 그대로가 아니라 '지금 무엇을 하는 중' 내레이션일 수 있다(2026-08-07) — 단계
    # 번호가 이미지 칸(3)을 가리키고 있는지가 이 테스트의 계약이다.
    assert seen and all(step == 3 and label for step, label in seen)


async def test_a_failed_draft_can_be_retried():
    """FAILED is terminal, so asking M4 again was refused and 다시 생성하기 did
    nothing. A model that timed out has not killed the post — the input and the
    chosen intent are still there."""
    generator = FailingDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    await service.generate_draft("post_1", {})
    failed = await repository.find_by_post_id("post_1")
    assert failed.status == BlogTaskStatus.FAILED
    assert failed.progress is None

    # Second try, with the generator working again.
    service._draft_generator = StubDraftGenerator()
    retried = await service.generate_draft("post_1", {})

    assert retried.status == BlogTaskStatus.READY_TO_PUBLISH
    assert retried.final_post.title == "Generated title"


async def test_explicit_legacy_images_are_generated_at_the_same_time():
    """명시된 구형 태그 이미지는 썸네일과 독립이므로 세 호출을 동시에 시작한다."""
    images = SlowImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(TAGGED_RESULT),
        post_image_generator=images,
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert len(updated.final_post.images) == 3
    assert images.peak_in_flight == 3


async def test_successful_draft_rescues_a_failed_empty_task_before_final_save():
    """A long image run can finish after another path has already marked the post
    FAILED. If this run has a complete draft and the failed task has no finalPost,
    keep the successful result instead of crashing on FAILED -> READY_TO_PUBLISH."""

    repository = InMemoryBlogTaskRepository()

    class RaceToFailedImageGenerator(StubImageGenerator):
        def __init__(self):
            super().__init__()
            self.flipped = False

        async def generate_post_image(self, image_input):
            if not self.flipped:
                self.flipped = True
                await repository.transition_status("post_1", BlogTaskStatus.FAILED, "tester")
            return await super().generate_post_image(image_input)

    service = DraftService(
        repository,
        StubDraftGenerator(),
        RaceToFailedImageGenerator(),
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post is not None


async def test_generation_failure_does_not_transition_failed_to_failed(caplog):
    caplog.set_level(logging.INFO, logger="app.modules.blog_task.jobs")
    repository = InMemoryBlogTaskRepository()

    class AlreadyFailedDraftGenerator:
        async def generate_draft(self, draft_input):
            await repository.transition_status("post_1", BlogTaskStatus.FAILED, "tester")
            raise RuntimeError("model failed after external failure")

    service = DraftService(
        repository,
        AlreadyFailedDraftGenerator(),
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.FAILED
    assert "FAILED to FAILED" not in caplog.text
    assert "background job failed" not in caplog.text
    assert "PIPELINE summary | phase=DRAFT post=post_1 ok=false" in caplog.text


async def test_tagged_images_are_generated_at_the_same_time():
    images = SlowImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(TAGGED_RESULT), post_image_generator=images
    )
    await repository.create(build_task())

    await service.generate_draft("post_1", {})

    # The cover and both body slots go out together — the cover does not wait on them.
    assert images.peak_in_flight == 3
    # Order still has to hold: an image belongs to the tag it replaced.
    assert [call.content_prompt for call in images.calls] == [None, "one", "two"]


async def test_generate_keeps_uploaded_reference_images_alongside_the_thumbnail():
    images = StubImageGenerator()
    repository, service = build_service(post_image_generator=images)
    await repository.create(
        build_task(
            input=BlogTaskInput(
                topic="블로그 자동화",
                keywords=["AI", "블로그"],
                reference_materials=[
                    ReferenceMaterial(
                        type=ReferenceMaterialType.IMAGE,
                        value=valid_image_data_url("PNG", (10, 20, 30)),
                    ),
                    ReferenceMaterial(
                        type=ReferenceMaterialType.IMAGE,
                        value=valid_image_data_url("JPEG", (40, 50, 60)),
                    ),
                ],
            )
        )
    )

    updated = await service.generate_draft("post_1", {})
    post = updated.final_post

    # An uploaded image cannot be the 대표 썸네일 — it is not thumbnail-spec and carries no copy.
    # They ride under the cover; an untagged draft does not invent body photos.
    assert [call.image_index for call in images.calls] == [0]
    assert [image.source for image in post.images] == [
        "generated",
        "reference",
        "reference",
    ]
    assert post.featured_image is post.images[0]


async def test_generate_replaces_image_tags_with_images_from_their_content_prompt():
    images = StubImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(TAGGED_RESULT), post_image_generator=images
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})
    post = updated.final_post

    # The first call is the cover, which has no tag behind it; the tags fill the rest.
    assert [call.content_prompt for call in images.calls] == [None, "one", "two"]
    assert len(post.images) == 3
    for content in (post.body, post.html_content, post.markdown_content):
        assert "[[IMAGE:" not in content
    # The image lands where its tag was, not at an arbitrary paragraph boundary.
    assert "![Generated image alt 2](data:image/jpeg;base64,abc121)\n\nB" in (
        post.markdown_content
    )


async def test_image_tag_alt_field_splits_korean_alt_from_the_english_scene():
    """`[[IMAGE: <영어> | alt=<한국어>]]` — 영어 장면만 이미지 모델로 가고, 한국어 alt는
    content_alt로 따로 전달된다. alt가 없는 옛 태그는 content_alt 없이 간다."""
    images = StubImageGenerator()
    tagged = DRAFT_RESULT.model_copy(
        update={
            "final_post": DRAFT_RESULT.final_post.model_copy(
                update={
                    "body": f"{GENERATED_BODY} [[IMAGE: one scene | alt=시장 좌판의 딸기]] B [[IMAGE: two scene]] C",
                    "html_content": f"<p>{GENERATED_BODY}</p><p>[[IMAGE: one scene | alt=시장 좌판의 딸기]]</p><p>B</p><p>[[IMAGE: two scene]]</p><p>C</p>",
                    "markdown_content": f"{GENERATED_BODY}\n\n[[IMAGE: one scene | alt=시장 좌판의 딸기]]\n\nB\n\n[[IMAGE: two scene]]\n\nC",
                }
            )
        }
    )
    repository, service = build_service(
        draft_generator=StubDraftGenerator(tagged), post_image_generator=images
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert [call.content_prompt for call in images.calls] == [None, "one scene", "two scene"]
    assert [call.content_alt for call in images.calls] == [None, "시장 좌판의 딸기", None]
    for content in (
        updated.final_post.body,
        updated.final_post.html_content,
        updated.final_post.markdown_content,
    ):
        assert "[[IMAGE:" not in content
        # 태그의 alt 필드가 본문에 글자 그대로 남으면 안 된다(<img alt=...>는 정상).
        assert "| alt=" not in content


async def test_the_cover_leads_the_post_and_carries_the_copy():
    images = StubImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(TAGGED_RESULT), post_image_generator=images
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})
    post = updated.final_post
    cover = images.calls[0]

    assert cover.is_thumbnail is True
    assert cover.thumbnail_copy == ["대표 문구", "두 줄까지"]
    assert post.thumbnail_copy == ["대표 문구", "두 줄까지"]
    # 네이버가 대표 이미지로 집어 가는 것은 글 맨 앞의 이미지다.
    assert post.featured_image is post.images[0]
    assert post.markdown_content.startswith("![Generated image alt 1](data:image/jpeg;base64,abc120)")
    assert not any(call.is_thumbnail for call in images.calls[1:])


async def test_a_draft_with_one_legacy_slot_gets_only_that_body_image():
    """구형 태그가 한 개면 그 장면만 만들고 관련 없는 장식 사진을 채우지 않는다."""
    one_tag = DRAFT_RESULT.model_copy(
        update={
            "final_post": DRAFT_RESULT.final_post.model_copy(
                update={
                    "body": f"A [[IMAGE: one]] B {GENERATED_BODY}",
                    "html_content": f"<p>A</p><p>[[IMAGE: one]]</p><p>B</p><p>{GENERATED_BODY}</p>",
                    "markdown_content": f"A\n\n[[IMAGE: one]]\n\nB\n\n{GENERATED_BODY}",
                }
            )
        }
    )
    images = StubImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(one_tag), post_image_generator=images
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})
    post = updated.final_post

    assert [call.content_prompt for call in images.calls] == [None, "one"]
    assert len(post.images) == 2
    for image in post.images:
        assert image.data_url in post.markdown_content
        assert image.data_url in post.html_content


async def test_tags_the_markdown_forgot_never_reach_the_reader():
    """M4가 markdownContent에만 태그를 빠뜨리면 태그 치환 경로를 타지 못한다. 그래도
    body와 htmlContent에 남은 `[[IMAGE:]]`가 발행된 글에 글자 그대로 찍혀서는 안 된다."""
    lopsided = DRAFT_RESULT.model_copy(
        update={
            "final_post": DRAFT_RESULT.final_post.model_copy(
                update={
                    "body": f"A [[IMAGE: one]] B {GENERATED_BODY}",
                    "html_content": f"<p>A</p><p>[[IMAGE: one]]</p><p>B</p><p>{GENERATED_BODY}</p>",
                    "markdown_content": f"A\n\nB\n\n{GENERATED_BODY}",
                }
            )
        }
    )
    images = StubImageGenerator()
    repository, service = build_service(
        draft_generator=StubDraftGenerator(lopsided), post_image_generator=images
    )
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})
    post = updated.final_post

    assert len(post.images) == 1
    for content in (post.body, post.html_content, post.markdown_content):
        assert "[[IMAGE:" not in content


async def test_every_image_in_one_post_shares_a_palette():
    """세 장이 따로 놀면 한 글의 이미지로 보이지 않는다. 색감은 글마다 하나로 묶인다."""
    images = StubImageGenerator()
    repository, service = build_service(post_image_generator=images)
    await repository.create(build_task())

    await service.generate_draft("post_1", {})

    styles = {call.visual_style for call in images.calls}
    assert len(styles) == 1 and styles != {None}


SEO_INTRO = (
    "아이폰17 출시일이 확정되면서 사양과 가격을 한눈에 정리했습니다. "
    "구매를 고민하는 독자가 실제로 궁금해할 내용을 미리 짚습니다."
)
SEO_PARAS = [
    f"{n}번 설명 문단입니다. 아이폰17 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다루고, "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 함께 얻습니다."
    # 22문단 + 도입 ≈ 2139자 — 기본(중간) 목표 1800~2300자 안이라 재작성이 돌지 않는다.
    for n in range(1, 23)
]
SEO_BODY = SEO_INTRO + "\n\n" + "\n\n".join(SEO_PARAS)
SEO_MARKDOWN = (
    "# 아이폰17 출시일 총정리\n\n"
    + SEO_INTRO
    + "\n\n## 출시일 정리\n\n"
    + "\n\n".join(SEO_PARAS[:8])
    + "\n\n## 가격 정리\n\n"
    + "\n\n".join(SEO_PARAS[8:15])
    + "\n\n## 주요 기능\n\n"
    + "\n\n".join(SEO_PARAS[15:])
)

SEO_RESULT = DRAFT_RESULT.model_copy(
    update={
        "final_post": DRAFT_RESULT.final_post.model_copy(
            update={
                "title": "아이폰17 출시일 총정리",
                "body": SEO_BODY,
                "html_content": (
                    "<article><h1>아이폰17 출시일 총정리</h1>"
                    f"<h2>출시일 정리</h2><p>{SEO_BODY}</p>"
                    "<h2>가격 정리</h2><p>정리</p><h2>주요 기능</h2><p>정리</p></article>"
                ),
                "markdown_content": SEO_MARKDOWN,
            }
        )
    }
)


class SeoAwareDraftGenerator:
    """제목 계획과 SEO 키워드 계획을 지원하는 스텁. 생성 전 단계가 실제로 도는지 본다."""

    def __init__(self, result: DraftGenerationResult = SEO_RESULT):
        self.result = result
        self.captured: list = []
        self.seo_calls = 0

    async def generate_title_plan(self, draft_input):
        return TitlePlan(
            primary_title="아이폰17 출시일 총정리",
            h1="아이폰17 출시일 총정리",
            primary_keyword="아이폰17",
            title_strategy="SEARCH_INTENT",
        )

    async def generate_seo_keyword_plan(self, draft_input):
        self.seo_calls += 1
        # 실어댑터와 같이 title_plan 기준으로 정규화(primary 고정)한다.
        return seo_keyword_plan_from_json(
            {
                "seoKeywordPlan": {
                    "primary": "전혀 다른 것",  # 제목에 없음 → title_plan.primary_keyword로 고정된다
                    "secondary": ["아이폰17 가격", "아이폰17 색상"],
                    "avoid": ["갤럭시 할인"],
                }
            },
            title_plan=draft_input.title_plan,
        )

    async def generate_draft(self, draft_input):
        self.captured.append(draft_input)
        return self.result


async def test_seo_keyword_plan_is_generated_saved_and_passed_to_the_prompt():
    """1단계 전체 왕복: SEO 계획을 만들어 DB에 저장하고, 원고 프롬프트 입력에 실어 보낸다."""
    generator = SeoAwareDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    # DB에 저장되었고, primary는 제목이 노리는 핵심 검색 구문으로 고정되었다.
    assert updated.seo_keyword_plan is not None
    assert updated.seo_keyword_plan.primary == "아이폰17"
    assert "아이폰17 가격" in updated.seo_keyword_plan.secondary
    # 원고 생성 프롬프트 입력에 계획이 실려 나갔다.
    assert generator.captured[0].seo_keyword_plan is not None
    assert generator.captured[0].seo_keyword_plan.primary == "아이폰17"


async def test_saved_seo_plan_is_reused_without_a_new_llm_call():
    """이미 저장된 SEO 계획이 있으면 그대로 재사용한다(title_plan과 같은 정책)."""
    generator = SeoAwareDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(
        build_task(
            title_plan=TitlePlan(
                primary_title="아이폰17 출시일 총정리",
                h1="아이폰17 출시일 총정리",
                primary_keyword="아이폰17",
                title_strategy="SEARCH_INTENT",
            ),
            seo_keyword_plan=seo_keyword_plan_from_json(
                {"seoKeywordPlan": {"primary": "아이폰17", "secondary": ["아이폰17 가격"], "avoid": []}}
            ),
        )
    )

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    # 저장된 계획을 재사용했으므로 SEO 생성 LLM은 호출되지 않는다.
    assert generator.seo_calls == 0
    assert generator.captured[0].seo_keyword_plan.primary == "아이폰17"


class SeoUnsatisfiableDraftGenerator:
    """SEO Primary가 제목·첫 문단에 끝내 안 들어가는 상황(제목 계획이 없어 앵커링도 없다).
    본문 자체(check_draft)는 멀쩡하다 — SEO만 못 지킨 경우다."""

    def __init__(self):
        self.draft_calls = 0

    async def generate_seo_keyword_plan(self, draft_input):
        # title_plan이 없어 primary가 제목 기준으로 고정되지 않는다 → 제목·본문에 없는 채로 남는다.
        return seo_keyword_plan_from_json(
            {"seoKeywordPlan": {"primary": "존재하지않는키워드", "secondary": [], "avoid": []}},
            title_plan=draft_input.title_plan,
        )

    async def generate_draft(self, draft_input):
        self.draft_calls += 1
        return SEO_RESULT


async def test_unmet_seo_primary_does_not_permanently_fail_generation():
    """Primary가 제목·첫 문단에 끝내 안 들어가도, 본문이 멀쩡하면 원고 생성은 실패하지 않는다.

    SEO Primary FAIL은 '다음 시도에서 고치라'는 재생성 신호이지 원고를 버릴 사유가 아니다.
    마지막 시도에서는 본문 자체가 규격을 지키면 원고를 내주고, 미충족은 로그로만 남긴다 —
    기존엔 나왔을 원고를 키워드 위치 하나 때문에 통째로 실패시키던 회귀를 막는다."""
    generator = SeoUnsatisfiableDraftGenerator()
    repository, service = build_service(draft_generator=generator)
    await repository.create(build_task())

    updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post is not None
    # 1차 시도의 SEO FAIL이 재생성을 유도해 정확히 두 번 생성했다(무한·영구 실패가 아니다).
    assert generator.draft_calls == 2
    # 계획은 저장됐고, 미충족 Primary가 그대로 기록된다.
    assert updated.seo_keyword_plan is not None
    assert updated.seo_keyword_plan.primary == "존재하지않는키워드"


class TrendTitleSeoGenerator:
    """사용자가 M2에서 제목을 고른 글. 제목 계획은 그 제목을 그대로 확정한다."""

    def __init__(self, title: str):
        self.title = title
        self.draft_calls = 0
        self.captured: list = []

    async def generate_title_plan(self, draft_input):
        return TitlePlan(
            primary_title=self.title,
            h1=self.title,
            primary_keyword="월드투어",
            title_strategy="TREND_CONNECTION",
        )

    async def generate_draft(self, draft_input):
        self.draft_calls += 1
        self.captured.append(draft_input)
        return SEO_RESULT.model_copy(
            update={
                "final_post": SEO_RESULT.final_post.model_copy(
                    update={"title": self.title}
                )
            }
        )


async def test_a_chosen_title_never_costs_a_wasted_regeneration(caplog):
    """사용자가 고른 제목에 없는 SEO primary가 저장돼 있어도 원고를 두 번 쓰지 않는다.

    제목은 M2에서 확정된 값이라 원고를 다시 써도 그대로다. 예전에는 그 제목에 primary가
    없다는 이유로 1차 시도가 항상 반려되어, 사용자를 기다리게 하는 원고 LLM 호출이 매번
    한 번씩 통째로 버려졌다(그리고 2차마저 다른 이유로 걸리면 생성 자체가 실패했다).
    이제는 쓰는 시점에 primary를 제목 안의 구문으로 맞춘다.
    """
    title = "월드투어 다시 시작하는 BTS, 일정과 배경 살펴보기"
    generator = TrendTitleSeoGenerator(title)
    repository, service = build_service(draft_generator=generator)
    await repository.create(
        build_task(
            trend_selection=TrendSelection(
                topic_candidate_id="topic_1",
                final_topic=title,
                selected_trend_keyword_ids=["trend_1"],
                skipped=False,
                selected_at=NOW,
            ),
            # 제목이 확정되기 전에 만들어져 저장된 계획: primary가 제목 어디에도 없다.
            seo_keyword_plan=seo_keyword_plan_from_json(
                {"seoKeywordPlan": {"primary": "BTS 월드투어", "secondary": [], "avoid": []}}
            ),
        )
    )

    with caplog.at_level(logging.INFO):
        updated = await service.generate_draft("post_1", {})

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert generator.draft_calls == 1
    # 쓰는 시점에 제목 안의 구문으로 맞춰졌고, 밀려난 키워드는 secondary로 남는다.
    used = generator.captured[0].seo_keyword_plan
    assert used.primary == "월드투어" and used.primary in title
    assert "BTS 월드투어" in used.secondary
    assert "SEO primary 재정렬" in caplog.text


async def test_generate_rejects_tasks_without_a_selected_intent():
    repository, service = build_service()
    await repository.create(build_task(status=BlogTaskStatus.SEARCH_ANALYZING, selected_intent=None))

    with pytest.raises(BlogTaskError):
        await service.generate_draft("post_1", {})


class TestUpdateDraftText:
    """The preview is a rich-text editor. What comes back is HTML, and it is user
    input: it gets copied into 네이버 and published, so it cannot be stored as it
    arrives."""

    async def test_the_edit_reaches_the_html_that_gets_published(self):
        repository, service = build_service()
        await repository.create(build_task())
        await service.generate_draft("post_1", {})

        updated = await service.update_draft_text(
            "post_1",
            {"title": "손으로 고친 제목", "html": "<p>첫 문단.</p><p>둘째 문단.</p>"},
        )
        post = updated.final_post

        assert post.title == "손으로 고친 제목"
        assert "손으로 고친 제목" in post.html_content
        assert "첫 문단." in post.html_content
        assert "Generated body" not in post.html_content
        # body and markdown are derived from the same HTML, so they cannot disagree.
        assert "첫 문단." in post.body
        assert "# 손으로 고친 제목" in post.markdown_content
        assert "둘째 문단." in post.markdown_content

    async def test_the_images_keep_the_place_the_user_dragged_them_to(self):
        images = StubImageGenerator()
        repository, service = build_service(
            draft_generator=StubDraftGenerator(TAGGED_RESULT),
            post_image_generator=images,
        )
        await repository.create(build_task())
        generated = await service.generate_draft("post_1", {})
        before = [image.data_url for image in generated.final_post.images]
        calls_before = len(images.calls)

        # The second image dragged above the first.
        updated = await service.update_draft_text(
            "post_1",
            {
                "title": "제목",
                "html": (
                    f'<img src="{before[1]}" alt="b" />'
                    "<p>사이 문단.</p>"
                    f'<img src="{before[0]}" alt="a" />'
                ),
            },
        )
        post = updated.final_post

        assert [image.data_url for image in post.images] == [before[1], before[0]]
        assert post.featured_image.data_url == before[1]
        assert post.html_content.index(before[1]) < post.html_content.index(before[0])
        # The image model was not called again.
        assert len(images.calls) == calls_before

    async def test_deleting_an_image_in_the_editor_drops_it(self):
        images = StubImageGenerator()
        repository, service = build_service(
            draft_generator=StubDraftGenerator(TAGGED_RESULT),
            post_image_generator=images,
        )
        await repository.create(build_task())
        generated = await service.generate_draft("post_1", {})
        before = [image.data_url for image in generated.final_post.images]
        assert len(before) >= 2

        updated = await service.update_draft_text(
            "post_1", {"title": "제목", "html": f'<p>본문.</p><img src="{before[0]}" alt="a" />'}
        )
        post = updated.final_post

        assert [image.data_url for image in post.images] == [before[0]]
        assert before[1] not in post.html_content

    async def test_script_is_dropped_not_escaped(self):
        """A <script> that survives as literal text in a published article is still
        wrong, just differently. It is not on the allowlist, so it does not survive
        at all."""
        repository, service = build_service()
        await repository.create(build_task())
        await service.generate_draft("post_1", {})

        updated = await service.update_draft_text(
            "post_1",
            {
                "title": "제목",
                "html": "<p>안전한 문단.</p><script>alert(1)</script><p onclick=\"evil()\">두번째</p>",
            },
        )
        html = updated.final_post.html_content

        assert "<script>" not in html
        assert "&lt;script&gt;" not in html
        assert "alert(1)" not in html
        assert "onclick" not in html
        assert "안전한 문단." in html
        assert "두번째" in html

    async def test_a_javascript_url_is_dropped(self):
        repository, service = build_service()
        await repository.create(build_task())
        await service.generate_draft("post_1", {})

        updated = await service.update_draft_text(
            "post_1",
            {"title": "제목", "html": '<p><a href="javascript:alert(1)">링크</a></p>'},
        )
        html = updated.final_post.html_content

        assert "javascript:" not in html
        # The words survive; only the link is gone.
        assert "링크" in html

    async def test_structure_survives_the_round_trip(self):
        repository, service = build_service()
        await repository.create(build_task())
        await service.generate_draft("post_1", {})

        updated = await service.update_draft_text(
            "post_1",
            {
                "title": "제목",
                "html": (
                    "<h2>소제목</h2><ul><li>하나</li><li>둘</li></ul>"
                    "<blockquote><p>인용</p></blockquote>"
                    '<p><a href="https://example.com">링크</a></p>'
                ),
            },
        )
        post = updated.final_post

        assert "<h2>소제목</h2>" in post.html_content
        assert "<li>하나</li>" in post.html_content
        assert "<blockquote>" in post.html_content
        assert 'href="https://example.com"' in post.html_content
        assert "## 소제목" in post.markdown_content
        assert "- 둘" in post.markdown_content

    async def test_an_empty_draft_is_refused(self):
        repository, service = build_service()
        await repository.create(build_task())
        await service.generate_draft("post_1", {})

        with pytest.raises(BlogTaskError) as error:
            await service.update_draft_text("post_1", {"title": "제목", "html": "   "})

        assert error.value.code == "VALIDATION_FAILED"


class TestQualityGate:
    """모델이 요구사항을 어기면 한 번 더 시킨다. 두 번 다 어기면 실패라고 말한다.

    같은 프롬프트에도 모델은 다른 답을 내놓으므로 재시도에 값이 있다. 다만 무한정
    시도하지는 않는다 — 짧은 원고를 조용히 내주는 것도, 사용자를 몇 분씩 기다리게 하는
    것도 답이 아니다.
    """

    async def test_a_short_draft_is_generated_again(self):
        short_post = DRAFT_RESULT.final_post.model_copy(
            update={"body": "검사하지 않는 값", "html_content": "<article><p>짧은 원고.</p></article>"}
        )
        short = DRAFT_RESULT.model_copy(update={"final_post": short_post})

        generator = SequenceDraftGenerator([short, DRAFT_RESULT])
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert generator.calls == 2
        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert updated.final_post.body == DRAFT_RESULT.final_post.body

    async def test_two_bad_drafts_fail_rather_than_ship(self):
        short_post = DRAFT_RESULT.final_post.model_copy(
            update={"body": "검사하지 않는 값", "html_content": "<article><p>짧은 원고.</p></article>"}
        )
        short = DRAFT_RESULT.model_copy(update={"final_post": short_post})

        generator = SequenceDraftGenerator([short, short])
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert generator.calls == 2
        # 900자짜리 원고를 화면에 올리느니 실패라고 말한다. 다시 생성하기로 재시도할 수 있다.
        assert updated.status == BlogTaskStatus.FAILED
        assert updated.final_post is None


class FlakyImageGenerator(StubImageGenerator):
    """처음 몇 번만 실패한다 — 이미지 실패 후 재실행이 어디서부터 다시 도는지 보기 위한 것."""

    def __init__(self, failures: int = 1):
        super().__init__()
        self.failures = failures

    async def generate_post_image(self, image_input):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("이미지 생성 실패(테스트)")
        return await super().generate_post_image(image_input)


class TestDraftCheckpointResume:
    """실패 후 '다시 실행하기'는 처음부터가 아니다 — 실패한 단계부터 다시 한다.

    본문(텍스트)까지 끝난 결과는 이미지 단계 전에 저장점으로 남고, 같은 입력의 재실행은
    그 본문을 재사용해 이미지부터 다시 시작한다. 입력이 바뀌면 저장점은 무시된다.
    """

    async def test_a_failed_image_stage_resumes_from_the_checkpoint(self):
        generator = StubDraftGenerator()
        repository, service = build_service(
            draft_generator=generator,
            post_image_generator=FlakyImageGenerator(failures=1),
        )
        await repository.create(build_task())

        failed = await service.generate_draft("post_1", {})
        assert failed.status == BlogTaskStatus.FAILED
        # 실패했지만 본문 저장점은 남아 있다 — 이것이 재개의 근거다.
        assert await repository.load_draft_checkpoint("post_1") is not None

        updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        # 본문 LLM은 첫 실행에서 한 번만 불렸다. 재실행은 저장점의 본문을 재사용했다.
        assert len(generator.captured) == 1
        # 성공한 글의 저장점은 지운다 — 다음 재생성이 옛 본문을 물려받으면 안 된다.
        assert await repository.load_draft_checkpoint("post_1") is None

    async def test_a_changed_input_invalidates_the_checkpoint(self):
        """스타일을 바꿔 다시 실행하면 본문도 다시 써야 한다 — 저장점을 재사용하지 않는다."""
        generator = StubDraftGenerator()
        repository, service = build_service(
            draft_generator=generator,
            post_image_generator=FlakyImageGenerator(failures=1),
        )
        await repository.create(build_task())

        failed = await service.generate_draft("post_1", {"style": "professional"})
        assert failed.status == BlogTaskStatus.FAILED

        updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert len(generator.captured) == 2

    async def test_an_old_prompt_checkpoint_is_invalidated_after_a_version_bump(
        self, monkeypatch
    ):
        """프롬프트/개인정보 계약이 바뀌면 옛 본문 저장점을 새 이미지 단계에 섞지 않는다."""
        generator = StubDraftGenerator()
        repository, service = build_service(
            draft_generator=generator,
            post_image_generator=FlakyImageGenerator(failures=1),
        )
        await repository.create(build_task())

        monkeypatch.setattr(
            draft_service_module, "M4_PROMPT_VERSION", "m4-draft@v2.3"
        )
        failed = await service.generate_draft("post_1", {})
        assert failed.status == BlogTaskStatus.FAILED
        old_checkpoint = await repository.load_draft_checkpoint("post_1")
        assert old_checkpoint is not None
        assert old_checkpoint.draft_input.prompt_version == "m4-draft@v2.3"

        monkeypatch.setattr(
            draft_service_module, "M4_PROMPT_VERSION", "m4-draft@v2.4"
        )
        updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert len(generator.captured) == 2
        assert generator.captured[-1].prompt_version == "m4-draft@v2.4"
        assert await repository.load_draft_checkpoint("post_1") is None

    async def test_a_deleted_task_drops_its_checkpoint(self):
        repository, service = build_service(
            post_image_generator=FlakyImageGenerator(failures=1)
        )
        await repository.create(build_task())
        failed = await service.generate_draft("post_1", {})
        assert failed.status == BlogTaskStatus.FAILED
        assert await repository.load_draft_checkpoint("post_1") is not None

        await repository.delete_by_post_id("post_1")

        assert await repository.load_draft_checkpoint("post_1") is None


class TestThumbnailSafetyFallback:
    """실존 인물 소재의 썸네일이 이미지 안전 시스템에 차단되면, 이름을 유지한 재시도는
    반드시 같은 이유로 죽는다. 그때만 고유 대상 없이 한 번 더 시도한다."""

    def setup_method(self):
        # 차단 이력은 프로세스 전역 기억이라(2026-08-10), 앞 테스트가 기억시킨 이름이
        # 뒤 테스트의 사전 우회를 켜지 않게 비운다.
        from app.modules.draft.service import _safety_blocked_identities

        _safety_blocked_identities.clear()

    async def test_a_safety_blocked_thumbnail_retries_without_the_named_subject(self):
        from app.modules.draft.card_selection import NamedSubject

        _, service = build_service()
        calls: list = []

        async def generate(subject):
            calls.append(subject)
            if subject is not None:
                raise RuntimeError(
                    'provider request failed with 400: {"code": "moderation_blocked"}'
                )
            return "이미지"

        named = NamedSubject(kind="REAL_PERSON", identity="프로미스나인")
        image = await service._generate_thumbnail_with_subject_fallback(named, generate)

        assert image == "이미지"
        assert calls == [named, None]

    async def test_a_non_safety_failure_keeps_the_prompt_and_fails(self):
        """혼잡·타임아웃은 프롬프트 문제가 아니다 — 대상을 지우지 않고 그대로 실패한다."""
        from app.modules.draft.card_selection import NamedSubject

        _, service = build_service()
        calls: list = []

        async def generate(subject):
            calls.append(subject)
            raise RuntimeError("provider request failed with 429: too many requests")

        named = NamedSubject(kind="REAL_PERSON", identity="프로미스나인")
        with pytest.raises(RuntimeError, match="429"):
            await service._generate_thumbnail_with_subject_fallback(named, generate)

        assert calls == [named]

    async def test_a_block_without_a_named_subject_gives_up_the_thumbnail(self):
        """이름 없는 프롬프트까지 차단되면 더 바꿀 것이 없다 — 같은 프롬프트의 재시도는
        반드시 같은 이유로 죽는다. 원고 전체를 FAILED로 만드는 대신 대표 이미지를
        포기한다(2026-08-10 새벽, 스파이더맨 글이 이 예외로 4연속 FAILED)."""
        _, service = build_service()
        calls: list = []

        async def generate(subject):
            calls.append(subject)
            raise RuntimeError(
                'provider request failed with 400: {"code": "moderation_blocked"}'
            )

        image = await service._generate_thumbnail_with_subject_fallback(None, generate)

        assert image is None
        assert calls == [None]

    async def test_a_double_safety_block_completes_without_a_thumbnail(self):
        """이름을 실은 시도와 이름 없는 시도가 모두 차단돼도 예외가 밖으로 나가지 않는다."""
        from app.modules.draft.card_selection import NamedSubject

        _, service = build_service()
        calls: list = []

        async def generate(subject):
            calls.append(subject)
            raise RuntimeError(
                'provider request failed with 400: {"code": "moderation_blocked"}'
            )

        named = NamedSubject(kind="REAL_PERSON", identity="프로미스나인")
        image = await service._generate_thumbnail_with_subject_fallback(named, generate)

        assert image is None
        assert calls == [named, None]
        # 차단된 이름은 기억한다 — 같은 실행의 다음 시도가 이름을 다시 싣지 않게.
        from app.modules.draft.service import _identity_safety_blocked

        assert _identity_safety_blocked("프로미스나인")


class TestSuppressedIdentityFallback:
    """'이름 없이' 폴백은 정말로 이름이 없어야 한다.

    named_subject를 비워도 subject_identity가 근거 anchor로, 확인된 특징이 fidelity로
    다시 채워지면 프롬프트에는 "The subject is specifically: (그 이름)"이 그대로 남아
    반드시 또 차단된다 — 2026-08-10 새벽 스파이더맨 글 4연속 FAILED의 원인.
    """

    def setup_method(self):
        # 앞 테스트가 기억시킨 차단 이름이 anchor 주입 판정을 바꾸지 않게 비운다.
        from app.modules.draft.service import _safety_blocked_identities

        _safety_blocked_identities.clear()

    async def test_suppression_drops_the_anchor_and_fidelity(self):
        capture = StubImageGenerator()
        repository, service = build_service(post_image_generator=capture)
        task = build_task()
        await repository.create(task)
        draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)
        evidence = build_profile([], topic="스파이더맨").model_copy(
            update={
                "primary_entity": "마블 시네마틱 유니버스(MCU) 스파이더맨",
                "confirmed_attributes": ["톰 홀랜드 주연"],
            }
        )
        draft_input = draft_input.model_copy(update={"reference_evidence": evidence})

        await service._generate_image(
            task,
            draft_input,
            DRAFT_RESULT.final_post,
            0,
            "차분한 색",
            1,
            is_thumbnail=True,
            suppress_subject_identity=True,
        )

        sent = capture.calls[-1]
        assert sent.subject_identity is None
        assert sent.fidelity_requirements == []
        # 프롬프트 끝까지 확인한다 — 소재 앵커("about: {topic}")로도 이름이 새지 않아야
        # 한다(이 줄 하나로 최종 폴백까지 차단돼 이미지 0장으로 완성된 실사례).
        from app.llm.prompts import image_prompt

        prompt = image_prompt(sent)
        assert "스파이더맨" not in prompt
        assert "마블" not in prompt
        assert "톰 홀랜드" not in prompt
        assert sent.input.topic not in prompt

    async def test_without_suppression_the_anchor_still_rides_along(self):
        """억제를 켜지 않은 기본 호출은 예전 그대로다 — 제품·장소 폴백이 anchor를 잃으면
        '그 제품'이 '관련된 아무 사진'이 된다."""
        capture = StubImageGenerator()
        repository, service = build_service(post_image_generator=capture)
        task = build_task()
        await repository.create(task)
        draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)
        evidence = build_profile([], topic="닷사이 23").model_copy(
            update={
                "primary_entity": "닷사이 23",
                "confirmed_attributes": ["정미율 23%"],
            }
        )
        draft_input = draft_input.model_copy(update={"reference_evidence": evidence})

        await service._generate_image(
            task,
            draft_input,
            DRAFT_RESULT.final_post,
            0,
            "차분한 색",
            1,
            is_thumbnail=True,
        )

        sent = capture.calls[-1]
        assert sent.subject_identity == "닷사이 23"
        assert sent.fidelity_requirements == ["정미율 23%"]


class TestPrefetchedStageCaches:
    """근거 추리·편집 스타일 모델 출력은 같은 입력이면 한 번만 부른다.

    선행 생성(prefetch)이 만든 결과를 실제 생성이 버리고 같은 LLM을 다시 부르던 것이
    1단계(원고 구조 설계)가 매번 통째로 다시 돌던 이유다(2026-08-10 사용자: 156초).
    """

    async def test_reference_evidence_llm_runs_once_for_the_same_input(self):
        calls = {"count": 0}

        class CountingGenerator(StubDraftGenerator):
            async def generate_reference_evidence(self, draft_input):
                calls["count"] += 1
                return await super().generate_reference_evidence(draft_input)

        repository, service = build_service(draft_generator=CountingGenerator())
        task = build_task(
            input=BlogTaskInput(
                topic="블로그 자동화",
                keywords=["AI"],
                reference_materials=[
                    ReferenceMaterial(
                        type=ReferenceMaterialType.TEXT, value="직접 써 본 메모"
                    )
                ],
            )
        )
        await repository.create(task)
        draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)

        first = await service._with_reference_evidence(draft_input)
        second = await service._with_reference_evidence(draft_input)

        assert calls["count"] == 1
        assert first.reference_evidence is not None
        assert second.reference_evidence is not None

    async def test_editorial_style_llm_runs_once_for_the_same_input(self):
        from app.modules.draft.editorial_style import normalize_style_plan

        model_plan = normalize_style_plan(
            None,
            post_id="post_1",
            revision=0,
            topic="블로그 자동화",
            subject=None,
            purposes=[],
            article_length="medium",
        )
        calls = {"count": 0}

        class CountingGenerator(StubDraftGenerator):
            async def generate_editorial_style_plan(self, draft_input):
                calls["count"] += 1
                return model_plan

        repository, service = build_service(draft_generator=CountingGenerator())
        task = build_task()
        await repository.create(task)
        draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)

        first = await service._with_editorial_style(draft_input, task)
        second = await service._with_editorial_style(draft_input, task)

        assert calls["count"] == 1
        assert first.editorial_style is not None
        assert second.editorial_style is not None

    async def test_a_failed_evidence_call_is_not_cached(self):
        """실패는 캐시하지 않는다 — 다음 시도가 새로 부른다."""
        calls = {"count": 0}

        class FlakyEvidenceGenerator(StubDraftGenerator):
            async def generate_reference_evidence(self, draft_input):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("일시 실패(테스트)")
                return await super().generate_reference_evidence(draft_input)

        repository, service = build_service(draft_generator=FlakyEvidenceGenerator())
        task = build_task(
            input=BlogTaskInput(
                topic="블로그 자동화",
                keywords=["AI"],
                reference_materials=[
                    ReferenceMaterial(
                        type=ReferenceMaterialType.TEXT, value="직접 써 본 메모"
                    )
                ],
            )
        )
        await repository.create(task)
        draft_input = await service._build_draft_input(task, None, DraftFormat.HTML)

        # 첫 호출은 실패를 삼키고 코드 판정만 쓴다(기존 동작 유지).
        first = await service._with_reference_evidence(draft_input)
        assert first.reference_evidence is not None
        # 두 번째 호출은 실패가 캐시되지 않았으므로 다시 부른다.
        await service._with_reference_evidence(draft_input)
        assert calls["count"] == 2


class TestCompletedResultIsNotThrownAway:
    """완성된 원고를 저장할 자리가 없을 때 버리지 않는다.

    실제로 일어난 일: 서버가 재시작하면서 복구 스위퍼가 GENERATING이던 글을
    INTENT_SELECTED로 되돌렸고(recovery.py), 그 사이 살아 있던 실행 두 개가 각각 3~4분
    걸려 원고를 완성했는데 저장 단계에서 둘 다 버려졌다. 화면은 0%인 채로 남았다.
    """

    async def test_a_result_is_saved_even_if_recovery_rewound_the_status(self):
        repository, service = build_service()
        await repository.create(build_task())
        # 실행이 끝나기 직전 상태다: 아무도 돌리고 있지 않다고 표시돼 있고 원고도 없다.
        await repository.transition_status(
            "post_1", BlogTaskStatus.GENERATING, "test"
        )
        await repository.transition_status(
            "post_1", BlogTaskStatus.INTENT_SELECTED, "recovery"
        )
        result = DraftGenerationResult(
            prompt_version="m4-draft@v2.0",
            provider="stub",
            model="stub",
            generated_at=NOW,
            final_post=FinalPost(
                title="살아남은 원고",
                body="본문",
                hashtags=["a"],
                html_content="<p>본문</p>",
            ),
        )

        await service._save_draft_generation_result("post_1", result)

        saved = await repository.find_by_post_id("post_1")
        assert saved.status == BlogTaskStatus.READY_TO_PUBLISH
        assert saved.final_post.title == "살아남은 원고"

    async def test_a_result_never_overwrites_a_draft_that_already_landed(self):
        """먼저 저장된 원고가 이긴다 — 늦게 끝난 두 번째 실행이 덮어쓰지 않는다."""
        repository, service = build_service()
        await repository.create(build_task())
        first = await service.generate_draft("post_1", {})
        assert first.status == BlogTaskStatus.READY_TO_PUBLISH

        late = DraftGenerationResult(
            prompt_version="m4-draft@v2.0",
            provider="stub",
            model="stub",
            generated_at=NOW,
            final_post=FinalPost(
                title="늦게 끝난 원고",
                body="본문",
                hashtags=["a"],
                html_content="<p>본문</p>",
            ),
        )
        await service._save_draft_generation_result("post_1", late)

        saved = await repository.find_by_post_id("post_1")
        assert saved.final_post.title == first.final_post.title


class TestSaveTimeoutIsNotProofOfFailure:
    """시간 초과는 '안 들어갔다'가 아니다.

    이 경로의 쓰기는 `ReturnDocument.AFTER`라 방금 쓴 문서를 통째로 되받는다. 그런데
    pymongo는 응답 **전체**에 대해 마감 시각을 한 번만 잡는다
    (`network_layer.receive_message`). 서버가 커밋을 끝내고도 되받는 도중에
    `socketTimeoutMS`를 넘기면 드라이버는 `NetworkTimeout`을 던진다 — 원고는 DB에
    멀쩡히 있는데 사용자에게는 '원고 생성 실패'로만 보인다.
    """

    @pytest.fixture(autouse=True)
    def _no_waiting(self, monkeypatch):
        """재시도 사이의 대기(2초·4초)를 없앤다. 여기서 재려는 것은 대기가 아니다."""
        from app.modules.draft import service as draft_service_module

        monkeypatch.setattr(draft_service_module, "SAVE_RETRY_SECONDS", 0)

    async def test_시도를_다_쓰고도_실제로_저장됐으면_성공으로_끝낸다(self):
        repository, service = build_service()
        await repository.create(build_task(status=BlogTaskStatus.GENERATING))

        # 쓰기는 매번 시간 초과로 보이지만, 첫 시도가 사실은 커밋됐다.
        real_save = repository.save_draft_generation_result
        attempts: list[int] = []

        async def timing_out_save(post_id, result, actor):
            attempts.append(1)
            if len(attempts) == 1:
                await real_save(post_id, result, actor)
            raise TimeoutError("응답을 받다가 60초를 넘겼습니다")

        repository.save_draft_generation_result = timing_out_save

        # 예외가 올라오지 않아야 한다.
        await service._store_generation_result("post_1", DRAFT_RESULT)

        assert len(attempts) == 3, "세 번은 다 해 보고 나서 되읽어야 한다"
        saved = await repository.find_by_post_id("post_1")
        assert saved.final_post.title == DRAFT_RESULT.final_post.title

    async def test_정말_저장이_안_됐으면_원래_오류를_그대로_올린다(self):
        """되읽어 보고 없으면 감추지 않는다 — 실패는 실패라고 말해야 한다."""
        repository, service = build_service()
        await repository.create(build_task(status=BlogTaskStatus.GENERATING))

        async def always_failing_save(post_id, result, actor):
            raise TimeoutError("응답을 받다가 60초를 넘겼습니다")

        repository.save_draft_generation_result = always_failing_save

        with pytest.raises(TimeoutError):
            await service._store_generation_result("post_1", DRAFT_RESULT)

    async def test_되읽기마저_실패하면_모른다고_보고_원래_오류를_올린다(self):
        """확인하지 못한 것을 성공이라고 말하지 않는다."""
        repository, service = build_service()
        await repository.create(build_task(status=BlogTaskStatus.GENERATING))

        async def always_failing_save(post_id, result, actor):
            raise TimeoutError("응답을 받다가 60초를 넘겼습니다")

        async def unreachable_find(post_id):
            raise TimeoutError("되읽기도 닿지 않습니다")

        repository.save_draft_generation_result = always_failing_save
        repository.find_by_post_id = unreachable_find

        with pytest.raises(TimeoutError, match="응답을 받다가"):
            await service._store_generation_result("post_1", DRAFT_RESULT)


class ReviewingDraftGenerator(StubDraftGenerator):
    """최종 검수(4단계)를 흉내 내는 생성기.

    회차마다 미리 정해 둔 지적 목록을 돌려준다. 실제 어댑터와 같은 자리(review_final_draft)에
    붙여 두어야 서비스가 그것을 실제로 부르는지 확인할 수 있다.
    """

    def __init__(self, result, rounds: list[list[FinalReviewIssue]]):
        super().__init__(result)
        self.rounds = rounds
        self.reviewed: list = []

    async def review_final_draft(self, draft_input, final_post):
        self.reviewed.append(final_post.body)
        index = min(len(self.reviewed) - 1, len(self.rounds) - 1)
        return self.rounds[index]


def _fact_issue(quote: str, replacement: str) -> FinalReviewIssue:
    return FinalReviewIssue(
        kind="fact",
        severity="critical",
        reason="조사 자료와 다릅니다",
        quote=quote,
        replacement=replacement,
    )


class TestFinalReview:
    """4단계는 DB 쓰기 한 번('결과 정리')에서 사실 검수로 바뀌었다(2026-08-05)."""

    async def test_a_clean_article_is_reviewed_once_and_left_alone(self):
        generator = ReviewingDraftGenerator(DRAFT_RESULT, [[]])
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert len(generator.reviewed) == 1
        assert updated.final_post.body == GENERATED_BODY
        assert updated.draft_generation_result.final_review.rounds == 1
        assert updated.draft_generation_result.final_review.applied == 0

    async def test_a_wrong_sentence_is_corrected_in_place(self):
        """원고를 다시 쓰지 않는다 — 지적된 자리만 바꾼다. 다시 쓰면 이미 만든 이미지와
        구성을 전부 잃는다."""
        # 다른 문단의 부분 문자열이 되지 않는 인용을 쓴다("1번 문단입니다."는 "11번
        # 문단입니다." 안에도 들어 있어 무엇이 바뀌었는지 확인할 수 없다).
        quote = "24번 문단입니다."
        generator = ReviewingDraftGenerator(
            DRAFT_RESULT, [[_fact_issue(quote, "마지막 문단입니다.")], []]
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        body = updated.final_post.body
        assert "마지막 문단입니다." in body
        assert quote not in body
        # 나머지 문단은 그대로다 — 원고를 다시 쓴 것이 아니라 그 자리만 바꿨다.
        assert "23번 문단입니다." in body
        assert body.count("문단입니다.") == 24
        assert updated.draft_generation_result.final_review.applied == 1

    async def test_review_stops_after_three_rounds(self):
        """고칠 것이 계속 나와도 세 번에서 멈춘다. 완성된 원고를 두고 모델 호출만
        쌓이는 것을 막는다(2026-08-05 사용자 결정)."""
        generator = ReviewingDraftGenerator(
            DRAFT_RESULT,
            [
                [_fact_issue("21번 문단입니다.", "스물한째 문단.")],
                [_fact_issue("22번 문단입니다.", "스물둘째 문단.")],
                [_fact_issue("23번 문단입니다.", "스물셋째 문단.")],
                [_fact_issue("24번 문단입니다.", "스물넷째 문단.")],
            ],
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert len(generator.reviewed) == 3
        review = updated.draft_generation_result.final_review
        assert review.rounds == 3
        assert review.applied == 3
        body = updated.final_post.body
        assert "스물셋째 문단." in body
        # 4회차는 돌지 않았으므로 그 지적은 반영되지 않았다.
        assert "24번 문단입니다." in body
        assert "스물넷째 문단." not in body

    async def test_review_stops_early_when_nothing_can_be_applied(self):
        """원고에서 문장을 못 찾으면 한 번 더 물어도 같은 답이 온다 — 회차만 태운다."""
        generator = ReviewingDraftGenerator(
            DRAFT_RESULT, [[_fact_issue("원고에 없는 문장", "무엇이든")]]
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert len(generator.reviewed) == 1
        review = updated.draft_generation_result.final_review
        assert review.applied == 0
        assert len(review.issues) == 1  # 반영하지 못한 지적은 기록에 남는다

    async def test_a_failing_review_never_costs_the_finished_article(self, caplog):
        """여기 도착한 원고는 이미 규격·SEO 검사를 통과한 완성본이다. 검수는 그 위에 얹은
        마무리이지 관문이 아니다."""

        class BrokenReviewer(StubDraftGenerator):
            async def review_final_draft(self, draft_input, final_post):
                raise RuntimeError("provider down")

        generator = BrokenReviewer(DRAFT_RESULT)
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        with caplog.at_level(logging.WARNING):
            updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert updated.final_post.body == GENERATED_BODY
        assert "최종 검수 실패" in caplog.text

    async def test_an_old_generator_without_a_reviewer_still_works(self):
        """review_final_draft가 없는 어댑터(구형·테스트 스텁)도 그대로 돈다."""
        repository, service = build_service(draft_generator=StubDraftGenerator(DRAFT_RESULT))
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert updated.draft_generation_result.final_review is None


class PolishingDraftGenerator(StubDraftGenerator):
    """문장 다듬기(5단계)를 흉내 내는 생성기. 실제 어댑터와 같은 자리에 붙여 둔다."""

    def __init__(self, result, edits: list[PolishEdit]):
        super().__init__(result)
        self.edits = edits
        self.polished: list = []
        self.experience_flags: list[bool] = []

    async def polish_final_draft(self, draft_input, final_post, *, has_experience_material=False):
        self.polished.append(final_post.body)
        self.experience_flags.append(has_experience_material)
        return self.edits


def _polish_edit(before: str, after: str, kind: str = "assistant_tone") -> PolishEdit:
    return PolishEdit(kind=kind, reason="블로그 문장이 아닙니다", before=before, after=after)


class TestPolish:
    """5단계는 사실 검수 뒤에 붙는 마무리다(2026-08-05). 사실은 그대로 두고 표현만 고친다."""

    async def test_an_awkward_sentence_is_rewritten_in_place(self):
        generator = PolishingDraftGenerator(
            DRAFT_RESULT, [_polish_edit("24번 문단입니다.", "24번 문단은 이렇게 맺습니다.")]
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        body = updated.final_post.body
        assert "24번 문단은 이렇게 맺습니다." in body
        assert "24번 문단입니다." not in body
        # 나머지 원고는 그대로다 — 다듬기는 원고를 다시 쓰는 일이 아니다.
        assert "23번 문단입니다." in body
        polish = updated.draft_generation_result.polish
        assert polish.applied == 1 and polish.rejected == 0
        assert polish.edits[0].applied is True

    async def test_the_polish_stage_runs_after_the_fact_review(self):
        """순서가 반대면 애써 다듬은 문장을 검수가 다시 갈아엎는다. 다듬기가 본 본문에는
        검수가 고친 문장이 이미 들어 있어야 한다."""

        class ReviewingAndPolishing(PolishingDraftGenerator):
            async def review_final_draft(self, draft_input, final_post):
                return [_fact_issue("24번 문단입니다.", "검수가 고친 문단입니다.")]

        generator = ReviewingAndPolishing(
            DRAFT_RESULT, [_polish_edit("23번 문단입니다.", "23번 문단은 이렇습니다.")]
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert "검수가 고친 문단입니다." in generator.polished[0]
        body = updated.final_post.body
        assert "검수가 고친 문단입니다." in body and "23번 문단은 이렇습니다." in body

    async def test_an_edit_that_would_change_a_number_is_dropped_and_recorded(self):
        """다듬기가 사실을 바꾸면 그건 다듬기가 아니다. 막았다는 사실은 결과에 남는다."""
        generator = PolishingDraftGenerator(
            DRAFT_RESULT, [_polish_edit("24번 문단입니다.", "25번 문단입니다.")]
        )
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert "24번 문단입니다." in updated.final_post.body
        polish = updated.draft_generation_result.polish
        assert polish.applied == 0 and polish.rejected == 1
        assert polish.edits[0].rejected_rule == "수치 변경"

    async def test_the_stage_is_told_whether_the_user_supplied_experience(self):
        """체험 자료가 없으면 다듬기가 체험담을 만들지 못하게 해야 한다 — 그 판단의
        입력이 실제로 전달되는지."""
        generator = PolishingDraftGenerator(DRAFT_RESULT, [])
        repository, service = build_service(draft_generator=generator)
        task = build_task()
        await repository.create(task)

        await service.generate_draft("post_1", {})

        assert generator.experience_flags == [False]

    async def test_a_failing_polish_never_costs_the_finished_article(self, caplog):
        class BrokenPolisher(StubDraftGenerator):
            async def polish_final_draft(self, draft_input, final_post, **kwargs):
                raise RuntimeError("provider down")

        generator = BrokenPolisher(DRAFT_RESULT)
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        with caplog.at_level(logging.WARNING):
            updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert updated.final_post.body == GENERATED_BODY
        assert updated.draft_generation_result.polish is None
        assert "문장 다듬기 실패" in caplog.text

    async def test_an_old_generator_without_a_polisher_still_works(self):
        """polish_final_draft가 없는 어댑터(구형·테스트 스텁)도 그대로 돈다."""
        repository, service = build_service(draft_generator=StubDraftGenerator(DRAFT_RESULT))
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
        assert updated.draft_generation_result.polish is None


class TestAlreadyRunningDraftIsWaitedFor:
    """이미 쓰고 있는 원고가 있으면 **거절하지 않고 기다린다.**

    예약 워커가 여기서 막혔다(2026-08-06 신고). 작업이 어떤 이유로든 다시 실행되면
    (재시도·제어 중단 뒤 재개) 앞 실행이 띄운 원고 잡이 아직 돌고 있는데, 워커는
    `generate_draft`에서 "이 글은 이미 원고를 생성하고 있습니다"를 받아 **작업을 실패로
    적었다.** 그 사이 원고는 정상적으로 끝나 저장됐다 — 발행 내역에는 '실패'가 남고
    글은 멀쩡히 완성돼 있는, 두 화면이 다른 말을 하는 상태다.
    """

    async def test_두_번째_호출은_거절되지_않고_완성된_원고를_받는다(self):
        release = asyncio.Event()

        class SlowGenerator:
            def __init__(self):
                self.calls = 0

            async def generate_draft(self, draft_input):
                self.calls += 1
                await release.wait()
                return DRAFT_RESULT

        generator = SlowGenerator()
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        first = asyncio.create_task(service.generate_draft("post_1", {}))
        # 첫 잡이 실제로 돌기 시작할 때까지 넘겨준다.
        while generator.calls == 0:
            await asyncio.sleep(0)

        second = asyncio.create_task(service.generate_draft("post_1", {}))
        await asyncio.sleep(0)
        release.set()
        done_first, done_second = await asyncio.gather(first, second)

        # 둘 다 완성된 같은 글을 받는다 — 두 번째가 예외로 죽지 않는다.
        assert done_first.status == BlogTaskStatus.READY_TO_PUBLISH
        assert done_second.status == BlogTaskStatus.READY_TO_PUBLISH
        assert done_second.final_post.title == "Generated title"
        # 그리고 **원고를 두 벌 쓰지 않는다** — 두 번째는 기다리기만 했다.
        assert generator.calls == 1

    async def test_HTTP_시작_요청은_여전히_거절한다(self):
        """`start_draft_generation`은 그대로다 — 그쪽은 두 번째 생성을 걸어 주면 안 된다."""
        release = asyncio.Event()

        class SlowGenerator:
            async def generate_draft(self, draft_input):
                await release.wait()
                return DRAFT_RESULT

        repository, service = build_service(draft_generator=SlowGenerator())
        await repository.create(build_task())

        running = asyncio.create_task(service.generate_draft("post_1", {}))
        while "post_1" not in service._running_drafts:
            await asyncio.sleep(0)

        with pytest.raises(BlogTaskError) as caught:
            await service.start_draft_generation("post_1", {})
        assert caught.value.code == "INVALID_STATUS_TRANSITION"
        assert "이미 원고를 생성" in caught.value.message

        release.set()
        await running


class SecondReviewer:
    """2차 품질 검수기(다른 모델). 실제 어댑터와 같은 자리에 붙는다."""

    def __init__(self, rounds: list[list[FinalReviewIssue]] | None = None, fails: bool = False):
        self.rounds = rounds or [[]]
        self.fails = fails
        self.reviewed: list = []

    async def review_final_draft(self, draft_input, final_post):
        self.reviewed.append(final_post.body)
        if self.fails:
            raise RuntimeError("2차 검수가 죽었다")
        index = min(len(self.reviewed) - 1, len(self.rounds) - 1)
        return self.rounds[index]


class TestSecondFinalReviewer:
    """2026-08-07 사용자 결정 — 원고를 쓴 모델과 다른 모델이 같은 원고를 한 번 더 본다.

    자기가 쓴 글을 자기가 보면 같은 자리를 같은 이유로 지나친다. 두 검수는 나란히 돌고
    지적은 합쳐진다.
    """

    async def test_both_reviewers_see_the_same_article(self):
        generator = ReviewingDraftGenerator(DRAFT_RESULT, [[]])
        second = SecondReviewer([[]])
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        await service.generate_draft("post_1", {})

        assert generator.reviewed == second.reviewed

    async def test_what_only_the_second_reviewer_found_is_still_fixed(self):
        """한쪽만 잡은 문제도 고쳐야 한다 — 그것이 두 모델을 쓰는 이유다."""
        quote = "24번 문단입니다."
        generator = ReviewingDraftGenerator(DRAFT_RESULT, [[], []])
        second = SecondReviewer([[_fact_issue(quote, "고쳐진 문단입니다.")], []])
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert "고쳐진 문단입니다." in updated.final_post.body
        assert quote not in updated.final_post.body
        assert updated.draft_generation_result.final_review.applied == 1

    async def test_a_dead_second_reviewer_does_not_lose_the_first_ones_work(self):
        """2차는 마무리이지 관문이 아니다. 죽으면 1차 결과로 계속한다."""
        quote = "24번 문단입니다."
        generator = ReviewingDraftGenerator(
            DRAFT_RESULT, [[_fact_issue(quote, "1차가 고친 문단입니다.")], []]
        )
        second = SecondReviewer(fails=True)
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert "1차가 고친 문단입니다." in updated.final_post.body
        assert updated.draft_generation_result.final_review.applied == 1

    async def test_without_a_second_reviewer_the_old_path_still_runs(self):
        """2차 검수기가 없는 배포(자격 증명 없음)도 예전과 똑같이 돈다."""
        generator = ReviewingDraftGenerator(DRAFT_RESULT, [[]])
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert len(generator.reviewed) == 1
        assert updated.final_post.body == GENERATED_BODY


class CritiqueDraftGenerator(StubDraftGenerator):
    """비평·통합을 지원하는 생성기 — 새 경로(비평 → 통합 재작성)를 태운다."""

    def __init__(self, result, improved=None, fail_critique=False, fail_integration=False):
        super().__init__(result)
        self.critiqued: list[str] = []
        self.integrated: list[tuple[str, str, str | None]] = []
        self.improved = improved
        self.fail_critique = fail_critique
        self.fail_integration = fail_integration

    async def critique_final_draft(self, draft_input, final_post, model_markdown):
        self.critiqued.append(model_markdown)
        if self.fail_critique:
            raise RuntimeError("1차 비평이 죽었다")
        return {
            "strengths": ["구성이 짜임새 있다"],
            "weaknesses": ["도입이 길다"],
            "improvements": ["도입을 두 문장으로 줄여라"],
            "imageFindings": [],
        }

    async def integrate_critiques(
        self, draft_input, final_post, model_markdown, review_a, review_b
    ):
        self.integrated.append((model_markdown, review_a, review_b))
        if self.fail_integration:
            raise RuntimeError("통합이 죽었다")
        improved = (
            self.improved
            if self.improved is not None
            else model_markdown.replace("1번 문단입니다", "첫 번째 문단입니다")
        )
        return {
            "decisions": [
                {"source": "A", "point": "도입이 길다", "adopted": True, "reason": "타당"},
                {"source": "B", "point": "과장 표현", "adopted": False, "reason": "자료 안의 사실"},
            ],
            "improvedMarkdown": improved,
        }


class CritiqueSecondReviewer:
    """2차 검토(다른 모델) 스텁. 실제 어댑터와 같은 자리에 붙는다."""

    def __init__(self, fails=False):
        self.fails = fails
        self.critiqued: list[str] = []

    async def critique_final_draft(self, draft_input, final_post, model_markdown):
        self.critiqued.append(model_markdown)
        if self.fails:
            raise RuntimeError("2차 비평이 죽었다")
        return {
            "strengths": [],
            "weaknesses": ["셋째 문단이 근거 없이 단정한다"],
            "improvements": ["자료의 수치를 인용해라"],
            "imageFindings": [{"imageIndex": 1, "problem": "본문과 다른 장면", "suggestion": "둘째 문단 뒤로"}],
        }


class TestDualCritique:
    """M4 마무리 새 경로(2026-08-07) — 두 모델이 각자 결론을 내고, 통합으로 다시 쓴다."""

    async def test_두_검토가_같은_원고를_받고_통합_결과가_최종본이_된다(self):
        generator = CritiqueDraftGenerator(DRAFT_RESULT)
        second = CritiqueSecondReviewer()
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        # 두 검토가 같은 원고(자리표 마크다운)를 받았다.
        assert generator.critiqued == second.critiqued
        # 통합은 두 검토를 다 받았다 — 2차의 지적이 담겨 있다.
        _, review_a, review_b = generator.integrated[0]
        assert "도입이 길다" in review_a
        assert review_b is not None and "근거 없이 단정" in review_b
        # 통합의 개선 원고가 최종본이다.
        assert "첫 번째 문단입니다" in updated.final_post.body
        assert "1번 문단입니다." not in updated.final_post.body.split("\n")[0]
        # 반영/미반영이 기록됐다.
        review = updated.draft_generation_result.final_review
        assert review.applied == 1
        assert review.error is None

    async def test_이차_검토가_죽으면_일차만으로_통합한다(self):
        generator = CritiqueDraftGenerator(DRAFT_RESULT)
        second = CritiqueSecondReviewer(fails=True)
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        _, _review_a, review_b = generator.integrated[0]
        assert review_b is None
        assert "첫 번째 문단입니다" in updated.final_post.body

    async def test_통합이_죽으면_원본을_그대로_쓰고_사유를_남긴다(self):
        generator = CritiqueDraftGenerator(DRAFT_RESULT, fail_integration=True)
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.final_post.body == GENERATED_BODY
        assert "통합 실패" in (updated.draft_generation_result.final_review.error or "")

    async def test_재작성이_길이_규격을_벗어나면_원본을_유지한다(self):
        # 통합이 글을 세 문단으로 뭉개 왔다 — 규격 재검사가 잡는다.
        generator = CritiqueDraftGenerator(DRAFT_RESULT, improved="# 제목\n\n짧아진 글입니다.")
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.final_post.body == GENERATED_BODY
        assert "길이" in (updated.draft_generation_result.final_review.error or "")

    async def test_두_비평이_모두_죽으면_원본을_그대로_쓴다(self):
        generator = CritiqueDraftGenerator(DRAFT_RESULT, fail_critique=True)
        second = CritiqueSecondReviewer(fails=True)
        repository, service = build_service(draft_generator=generator, final_reviewer=second)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.final_post.body == GENERATED_BODY
        assert generator.integrated == []  # 통합을 부르지도 않는다
        assert "모두 실패" in (updated.draft_generation_result.final_review.error or "")

    async def test_재작성이_반영되면_별도_문장_다듬기를_건너뛴다(self):
        """재작성 프롬프트가 다듬기 지침을 안고 원고 전체를 다시 쓴다 — 그 위에 또
        전문을 읽는 다듬기 호출을 얹으면 4단계의 순차 LLM 대기만 하나 는다
        (2026-08-07 사용자: 4단계가 4분 넘게 걸린다)."""

        class CritiquingAndPolishing(CritiqueDraftGenerator):
            def __init__(self, result, **kwargs):
                super().__init__(result, **kwargs)
                self.polish_calls = 0

            async def polish_final_draft(
                self, draft_input, final_post, *, has_experience_material=False
            ):
                self.polish_calls += 1
                return []

        generator = CritiquingAndPolishing(DRAFT_RESULT)
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert "첫 번째 문단입니다" in updated.final_post.body  # 재작성이 반영됐다
        assert updated.draft_generation_result.final_review.mode == "critique-rewrite"
        assert generator.polish_calls == 0
        assert updated.draft_generation_result.polish is None

    async def test_재작성이_거절되면_다듬기가_예전대로_돈다(self):
        """원본을 유지한 글(mode 없음)은 아무도 표현을 다듬지 않은 상태다 — 다듬기
        호출을 건너뛰면 안 된다."""

        class CritiquingAndPolishing(CritiqueDraftGenerator):
            def __init__(self, result, **kwargs):
                super().__init__(result, **kwargs)
                self.polish_calls = 0

            async def polish_final_draft(
                self, draft_input, final_post, *, has_experience_material=False
            ):
                self.polish_calls += 1
                return []

        generator = CritiquingAndPolishing(DRAFT_RESULT, fail_integration=True)
        repository, service = build_service(draft_generator=generator)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.final_post.body == GENERATED_BODY
        assert updated.draft_generation_result.final_review.mode is None
        assert generator.polish_calls == 1


class TestOneDraftAtATimePerPerson:
    """한 사람의 **대화형** 원고 생성은 하나뿐이다(2026-08-12 사용자 지시).

        "백그라운드에서 작업이 돌 수 있는 것은 새로고침해도 현재 작업을 이어서 계속
        진행하는 것이고, 작업큐에 넘어가서 예약작업을 진행하고 있으며 새로운 소재를
        넣어서 작업을 진행하려고 하는 것에 대해서만 동시 실행이 되어야하는거야"

    한 편이 5~8분 걸리고 크롬 프로필도 하나라, 둘이 겹치면 둘 다 느려지고 어느 쪽이
    무엇을 하는지 화면에서 읽을 수 없다.
    """

    @staticmethod
    def _blocking_generator():
        import asyncio

        started = asyncio.Event()
        release = asyncio.Event()

        class Blocks:
            async def generate_draft(self, draft_input):
                started.set()
                await release.wait()
                return DRAFT_RESULT

        return Blocks(), started, release

    async def test_a_second_post_of_the_same_person_is_refused(self):
        repository, service = build_service()
        generator, started, release = self._blocking_generator()
        service._draft_generator = generator
        await repository.create(build_task(post_id="post_1", user_id="user_1"))
        await repository.create(build_task(post_id="post_2", user_id="user_1"))

        await service.start_draft_generation("post_1", {}, owner_id="user_1")
        await started.wait()

        with pytest.raises(BlogTaskError) as caught:
            await service.start_draft_generation("post_2", {}, owner_id="user_1")
        assert caught.value.code == "INVALID_STATUS_TRANSITION"
        assert "다른 글의 원고를 만들고" in caught.value.message

        release.set()
        await service._jobs.drain()

    async def test_another_person_is_not_blocked(self):
        """남의 글이 도는 것과 내 글은 상관이 없다."""
        repository, service = build_service()
        generator, started, release = self._blocking_generator()
        service._draft_generator = generator
        await repository.create(build_task(post_id="post_1", user_id="user_1"))
        await repository.create(build_task(post_id="post_2", user_id="user_2"))

        await service.start_draft_generation("post_1", {}, owner_id="user_1")
        await started.wait()

        # 거부되지 않는다.
        await service.start_draft_generation("post_2", {}, owner_id="user_2")

        release.set()
        await service._jobs.drain()

    async def test_the_scheduled_path_does_not_take_the_lock(self):
        """예약 작업은 ``generate_draft``로 들어와 이 잠금을 지나지 않는다.

        사용자가 명시한 예외다 — 예약이 도는 동안 새 소재로 글을 쓰는 것은 막지 않는다.
        여기서는 **잠금을 잡는 자리가 하나뿐**임을 확인한다.
        """
        import inspect

        from app.modules.draft.service import DraftService

        takes = inspect.getsource(DraftService.start_draft_generation)
        assert "_running_draft_owners[post_id] = owner_id" in takes
        # generate_draft는 잠금 장부를 건드리지 않는다.
        assert "_running_draft_owners" not in inspect.getsource(DraftService.generate_draft)

    async def test_the_lock_is_released_when_the_run_ends(self):
        """끝나면 장부에서 빠진다 — 안 그러면 그 사람은 다시는 시작하지 못한다."""
        repository, service = build_service()
        await repository.create(build_task(post_id="post_1", user_id="user_1"))

        await service.start_draft_generation("post_1", {}, owner_id="user_1")
        await service._jobs.drain()

        assert service._running_draft_owners == {}
