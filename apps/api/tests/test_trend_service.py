"""M2 service: keyword collection, then titles for the selected keyword."""

import asyncio

import pytest

from app.errors import BlogTaskError
from app.llm import (
    TitleEvaluationInput,
    TitleJudgment,
    TopicGenerationInput,
    TopicRecommendationResult,
    TrendFetchResult,
)
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.persona import InMemoryPersonaRepository, PersonaService
from app.modules.trend.service import TrendService
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    IntentCandidate,
    IntentValidationResult,
    SelectedIntent,
    StatusHistoryEntry,
    TopicCandidate,
    TrendKeyword,
    TrendSelection,
    TrendSource,
    UserSettings,
)

NOW = "1970-01-01T00:00:00.000Z"


def keyword(keyword_id: str = "trend_1", text: str = "AI 검색") -> TrendKeyword:
    return TrendKeyword(
        trend_keyword_id=keyword_id,
        keyword=text,
        source=TrendSource.GOOGLE_TRENDS,
        rank=1,
        score=98,
        collected_at=NOW,
    )


class StubTrendProvider:
    def __init__(self):
        self.last_input = None

    async def fetch_trends(self, trend_input):
        self.last_input = trend_input
        return TrendFetchResult(
            collected_at=NOW,
            mode=trend_input.mode,
            trend_keywords=[keyword("trend_1", "AI 검색"), keyword("trend_2", "민생지원금")],
        )


class StubTopicGenerator:
    """Records the input so the persona/keyword/exclusion plumbing can be asserted."""

    def __init__(self, titles: list[str] | None = None, error: Exception | None = None):
        self.calls: list[TopicGenerationInput] = []
        self._titles = titles if titles is not None else ["AI 검색 모르면 손해입니다"]
        self._error = error

    async def generate_topics(self, topic_input: TopicGenerationInput):
        self.calls.append(topic_input)
        if self._error:
            raise self._error
        return TopicRecommendationResult(
            generated_at=NOW,
            topic_candidates=[
                TopicCandidate(
                    topic_candidate_id=f"topic_{index + 1}",
                    title=title,
                    description="폭로형",
                    # Deliberately wrong: the service must re-pin every candidate
                    # to the keyword the user actually selected.
                    trend_keyword_ids=["some_other_keyword"],
                    recommended=False,
                )
                for index, title in enumerate(self._titles)
            ],
        )


class StubTopicEvaluator:
    """제목별 점수를 고정 표로 돌려주는 배치 평가기. 없는 제목은 중립값."""

    def __init__(self, scores: dict[str, TitleJudgment]):
        self._scores = scores
        self.calls: list[TitleEvaluationInput] = []

    async def evaluate_titles(self, evaluation_input: TitleEvaluationInput):
        self.calls.append(evaluation_input)
        return {
            title: self._scores.get(
                title,
                TitleJudgment(
                    relevance=50,
                    trend_reflection=50,
                    purpose_match=50,
                    audience_interest=50,
                ),
            )
            for title in evaluation_input.titles
        }


class StubUserSettingsService:
    def __init__(self, settings: UserSettings | None = None):
        self._settings = settings

    async def get_by_user_id(self, user_id: str) -> UserSettings | None:
        return self._settings


def build_settings(persona: str = "p_6") -> UserSettings:
    return UserSettings(
        user_id="user_1",
        hashtag_count=7,
        default_persona=persona,
        auto_posting_enabled=False,
        created_at=NOW,
        updated_at=NOW,
    )


def build_task(**overrides) -> BlogTask:
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        status=BlogTaskStatus.REFERENCE_PROCESSING,
        version=2,
        created_at=NOW,
        updated_at=NOW,
        status_history=[
            StatusHistoryEntry(
                **{
                    "from": BlogTaskStatus.INPUT,
                    "to": BlogTaskStatus.REFERENCE_PROCESSING,
                    "at": NOW,
                    "by": "system:m1-reference-intake",
                }
            )
        ],
        input=BlogTaskInput(
            topic="블로그 자동화",
            purpose=["후기·리뷰 작성"],
            keywords=["후기·리뷰 작성"],
            reference_materials=[],
        ),
        posting_logs=[],
    )
    return BlogTask(**{**defaults, **overrides})


def build_service(
    task: BlogTask | None = None,
    topics: StubTopicGenerator | None = None,
    settings: UserSettings | None = None,
    evaluator: StubTopicEvaluator | None = None,
):
    repository = InMemoryBlogTaskRepository()
    generator = topics or StubTopicGenerator()
    service = TrendService(
        repository=repository,
        trend_provider=StubTrendProvider(),
        topic_generator=generator,
        topic_evaluator=evaluator,
        user_settings_service=StubUserSettingsService(settings),
        persona_service=PersonaService(InMemoryPersonaRepository()),
    )
    return repository, service, generator, task or build_task()


async def test_recommend_returns_keywords_without_changing_status():
    repository, service, generator, task = build_service()
    await repository.create(task)

    result = await service.recommend_topics(task.post_id, {"maxKeywords": 3})
    stored = await repository.find_by_post_id(task.post_id)

    assert result.post_id == task.post_id
    assert len(result.trend_keywords) == 2
    assert stored.status == BlogTaskStatus.REFERENCE_PROCESSING


async def test_recommend_threads_the_mode_to_the_provider_and_back():
    """추천어/소재 관련어 탭이 보낸 mode가 수집기까지 전달되고 응답에도 그대로 실린다."""
    repository = InMemoryBlogTaskRepository()
    provider = StubTrendProvider()
    service = TrendService(
        repository=repository,
        trend_provider=provider,
        topic_generator=StubTopicGenerator(),
        user_settings_service=StubUserSettingsService(),
    )
    task = build_task()
    await repository.create(task)

    result = await service.recommend_topics(task.post_id, {"mode": "MATERIAL_RELATED"})

    assert provider.last_input.mode.value == "MATERIAL_RELATED"
    assert result.mode.value == "MATERIAL_RELATED"


async def test_recommend_defaults_to_the_trending_mode():
    repository, service, _, task = build_service()
    await repository.create(task)

    result = await service.recommend_topics(task.post_id, {})

    assert result.mode.value == "TRENDING"


async def test_recommend_rejects_an_unknown_mode():
    repository, service, _, task = build_service()
    await repository.create(task)

    with pytest.raises(BlogTaskError, match="mode"):
        await service.recommend_topics(task.post_id, {"mode": "HOTTEST"})


async def test_recommend_does_not_spend_a_title_model_call():
    """Opening the 제목 panel used to write titles for the hottest keyword, which
    the user had not chosen and might never choose. Titles are written when 제목
    추천 is pressed, for the keyword that is actually selected."""
    repository, service, generator, task = build_service()
    await repository.create(task)

    result = await service.recommend_topics(task.post_id, {})

    assert generator.calls == []
    assert result.topic_candidates == []


async def test_titles_are_written_in_the_users_persona():
    repository, service, generator, task = build_service(settings=build_settings())
    await repository.create(task)

    await service.generate_topics(
        task.post_id,
        {"trendKeywordId": "trend_1", "keyword": "AI 검색", "source": "GOOGLE_TRENDS"},
    )

    # 문구 개정에 흔들리지 않게 페르소나 이름으로 시작하는지만 고정한다(id→프롬프트 배선 검증).
    assert generator.calls[0].settings.default_persona.startswith("트렌드 에디터")
    assert generator.calls[0].settings.hashtag_count == 7
    # The purpose picked in the 주제 step reaches the title prompt too.
    assert generator.calls[0].input.purpose == ["후기·리뷰 작성"]


async def test_generate_topics_rewrites_the_list_for_the_selected_keyword():
    repository, service, generator, task = build_service()
    await repository.create(task)

    result = await service.generate_topics(
        task.post_id,
        {
            "trendKeywordId": "trend_2",
            "keyword": "민생지원금",
            "source": "GOOGLE_TRENDS",
            "excludeTitles": ["이미 본 제목"],
        },
    )

    assert result.trend_keyword_id == "trend_2"
    assert generator.calls[0].trend_keyword.keyword == "민생지원금"
    # Pressing 제목 추천 again must not hand back what is already on screen.
    assert generator.calls[0].exclude_titles == ["이미 본 제목"]
    for candidate in result.topic_candidates:
        assert candidate.trend_keyword_ids == ["trend_2"]


async def test_recommended_title_is_chosen_by_rubric_score_not_first():
    """추천은 index==0(첫 후보)이 아니라 루브릭 최고점이다. 첫 후보가 낚시·소재 누락이면
    추천되지 않고, 소재·트렌드를 담은 깔끔한 후보가 추천된다. 평가기 없이 규칙만으로도 성립한다."""
    generator = StubTopicGenerator(
        titles=[
            "충격 대박 무조건 클릭하세요",  # 낚시 + 소재·트렌드 없음
            "블로그 자동화에 트렌드키워드 활용하는 법",  # 소재(자동화)·트렌드 포함, 깔끔
            "자동화 소개",  # 짧고 트렌드 없음
        ]
    )
    repository, service, _, task = build_service(topics=generator)
    await repository.create(task)

    result = await service.generate_topics(
        task.post_id,
        {"trendKeywordId": "trend_1", "keyword": "트렌드키워드", "source": "GOOGLE_TRENDS"},
    )

    recommended = [c for c in result.topic_candidates if c.recommended]
    assert len(recommended) == 1
    assert recommended[0].title == "블로그 자동화에 트렌드키워드 활용하는 법"
    # 모든 후보에 점수와 근거 한 줄이 붙는다(상세 점수는 감춰도 되지만 근거는 표시).
    assert all(c.score is not None for c in result.topic_candidates)
    assert all(c.reason for c in result.topic_candidates)


async def test_llm_evaluation_drives_the_recommendation():
    """LLM 배치 평가가 있으면 그 점수가 의미 판단 항을 채워 추천을 정하고, 근거도 그대로 표시된다."""
    winner = "트렌드키워드로 블로그 자동화 시작하는 법"
    generator = StubTopicGenerator(
        titles=["자동화와 트렌드키워드로 여는 아침 루틴", winner]
    )
    evaluator = StubTopicEvaluator(
        {
            winner: TitleJudgment(
                relevance=95,
                trend_reflection=95,
                purpose_match=95,
                audience_interest=95,
                reason="소재와 트렌드를 자연스럽게 연결합니다",
            )
        }
    )
    repository, service, _, task = build_service(topics=generator, evaluator=evaluator)
    await repository.create(task)

    result = await service.generate_topics(
        task.post_id,
        {"trendKeywordId": "trend_1", "keyword": "트렌드키워드", "source": "GOOGLE_TRENDS"},
    )

    recommended = next(c for c in result.topic_candidates if c.recommended)
    assert recommended.title == winner
    assert recommended.reason == "소재와 트렌드를 자연스럽게 연결합니다"
    # 생성된 제목들이 평가기로 넘어갔다(생성과 평가 분리).
    assert evaluator.calls and winner in evaluator.calls[0].titles


async def test_recommend_is_unaffected_by_a_dead_title_model():
    """The title model is no longer on this path at all, so it cannot take the
    keyword panel down with it."""
    generator = StubTopicGenerator(error=RuntimeError("anthropic 529"))
    repository, service, _, task = build_service(topics=generator)
    await repository.create(task)

    result = await service.recommend_topics(task.post_id, {})

    assert len(result.trend_keywords) == 2
    assert result.topic_candidates == []


async def test_generate_topics_surfaces_a_title_model_failure():
    """The user pressed a button whose only job is titles. Quietly handing back
    templates would misreport what happened."""
    generator = StubTopicGenerator(error=RuntimeError("anthropic 529"))
    repository, service, _, task = build_service(topics=generator)
    await repository.create(task)

    with pytest.raises(RuntimeError, match="529"):
        await service.generate_topics(
            task.post_id, {"trendKeywordId": "trend_1", "keyword": "AI 검색"}
        )


async def test_generate_topics_rejects_a_missing_keyword():
    repository, service, _, task = build_service()
    await repository.create(task)

    with pytest.raises(BlogTaskError, match="keyword"):
        await service.generate_topics(task.post_id, {"trendKeywordId": "trend_1"})


async def test_select_topic_stores_the_selection_and_advances():
    repository, service, _, task = build_service()
    await repository.create(task)

    updated = await service.select_topic(
        task.post_id,
        {
            "topicCandidateId": "topic_1",
            "finalTopic": "AI 검색 모르면 손해입니다",
            "selectedTrendKeywordIds": ["trend_1"],
            "skipped": False,
        },
    )

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert updated.trend_selection.final_topic == "AI 검색 모르면 손해입니다"
    assert updated.trend_selection.selected_trend_keyword_ids == ["trend_1"]
    assert updated.status_history[-1].by == "system:m2-trend-selection"


async def test_select_topic_can_skip_and_keep_the_original_topic():
    repository, service, _, task = build_service()
    await repository.create(task)

    updated = await service.select_topic(task.post_id, {"skipped": True})

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert updated.trend_selection.skipped is True
    assert updated.trend_selection.final_topic == task.input.topic


async def test_recommend_rejects_tasks_that_are_not_ready_for_m2():
    repository, service, _, task = build_service(build_task(status=BlogTaskStatus.INPUT))
    await repository.create(task)

    with pytest.raises(BlogTaskError):
        await service.recommend_topics(task.post_id, {})


def _picked_selection(final_topic: str = "첫 제목") -> TrendSelection:
    return TrendSelection(
        topic_candidate_id="topic_1",
        final_topic=final_topic,
        selected_trend_keyword_ids=["trend_1"],
        selected_keywords=["AI 검색"],
        skipped=False,
        selected_at=NOW,
    )


def _validation_result() -> IntentValidationResult:
    return IntentValidationResult(
        prompt_version="m3-intent@v1.1",
        provider="stub",
        model="stub",
        analyzed_at=NOW,
        intent_candidates=[
            IntentCandidate(
                intent_id="intent_1",
                title="첫 제목",
                target_reader="독자",
                rationale="근거",
                keywords=[],
                sources=[],
            )
        ],
    )


async def test_select_topic_can_be_repicked_before_the_intent_is_confirmed():
    """제목을 고른 뒤(SEARCH_ANALYZING)에도 방향 확정 전에는 다시 고를 수 있다.

    다시 고르면 옛 제목으로 만든 검증 결과는 함께 버려진다 — 남겨 두면 새 제목의 검증
    팝업이 옛 제목의 방향·자료를 보여준다.
    """
    repository, service, _, task = build_service(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING,
            trend_selection=_picked_selection(),
            intent_validation_result=_validation_result(),
        )
    )
    await repository.create(task)

    updated = await service.select_topic(
        task.post_id,
        {
            "topicCandidateId": "topic_2",
            "finalTopic": "두 번째 제목",
            "selectedTrendKeywordIds": ["trend_2"],
            "skipped": False,
        },
    )

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert updated.trend_selection.final_topic == "두 번째 제목"
    assert updated.intent_validation_result is None


async def test_recommend_and_topics_still_work_after_a_title_was_picked():
    """검증 팝업의 '수정하기'로 제목 단계에 돌아온 상태(SEARCH_ANALYZING)에서 키워드
    수집과 제목 생성이 다시 된다."""
    repository, service, _, task = build_service(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING,
            trend_selection=_picked_selection(),
        )
    )
    await repository.create(task)

    recommended = await service.recommend_topics(task.post_id, {"maxKeywords": 3})
    assert len(recommended.trend_keywords) == 2

    topics = await service.generate_topics(
        task.post_id, {"trendKeywordId": "trend_1", "keyword": "AI 검색"}
    )
    assert topics.topic_candidates


async def test_m2_is_locked_once_the_intent_is_confirmed():
    """방향을 확정한 뒤에는 원고가 그 제목으로 쓰이므로 제목을 되돌릴 수 없다."""
    confirmed = build_task(
        status=BlogTaskStatus.INTENT_SELECTED,
        trend_selection=_picked_selection(),
        selected_intent=SelectedIntent(
            intent_id="intent_1",
            title="첫 제목",
            target_reader="독자",
            rationale="근거",
        ),
    )
    repository, service, _, task = build_service(confirmed)
    await repository.create(task)

    with pytest.raises(BlogTaskError, match="INVALID_STATUS_TRANSITION|M2 requires"):
        await service.select_topic(
            task.post_id,
            {
                "topicCandidateId": "topic_2",
                "finalTopic": "두 번째 제목",
                "selectedTrendKeywordIds": ["trend_2"],
                "skipped": False,
            },
        )


# ------------------------------------------------------- 키워드 선행 수집


class CountingTrendProvider(StubTrendProvider):
    """수집 횟수를 센다. 선행분이 실제로 재사용되는지 보려면 이 숫자가 답이다."""

    def __init__(self, gate: "asyncio.Event | None" = None):
        super().__init__()
        self.calls: list[str] = []
        self._gate = gate

    async def fetch_trends(self, trend_input):
        self.calls.append(trend_input.mode.value)
        if self._gate is not None:
            await self._gate.wait()
        return await super().fetch_trends(trend_input)


def build_prefetch_service(provider):
    repository = InMemoryBlogTaskRepository()
    service = TrendService(
        repository=repository,
        trend_provider=provider,
        topic_generator=StubTopicGenerator(),
        user_settings_service=StubUserSettingsService(),
    )
    return repository, service


async def test_입력을_저장하면_두_탭의_키워드를_미리_모은다():
    """2026-08-07 사용자 요청 — '다음'을 누른 순간 소재·참고자료는 이미 서버에 있다.
    제목 화면이 뜨기를 기다릴 이유가 없다."""
    provider = CountingTrendProvider()
    repository, service = build_prefetch_service(provider)
    task = build_task()
    await repository.create(task)

    service.start_keyword_prefetch(task)
    await service._jobs.drain()

    # 화면이 처음 여는 최신순과, 사용자가 눌러서 보는 소재 관련순 둘 다.
    assert sorted(provider.calls) == ["MATERIAL_RELATED", "TRENDING"]


async def test_화면의_요청은_돌고_있는_선행_수집에_붙는다():
    """붙지 않으면 같은 수집이 두 번 돈다 — 소재 관련순은 LLM 판정까지 두 벌 쓴다."""
    gate = asyncio.Event()
    provider = CountingTrendProvider(gate)
    repository, service = build_prefetch_service(provider)
    task = build_task()
    await repository.create(task)

    service.start_keyword_prefetch(task)
    # 선행이 수집기 안에서 붙잡혀 있는 동안 화면이 같은 요청을 보낸다.
    await asyncio.sleep(0)
    asking = asyncio.ensure_future(
        service.recommend_topics(
            task.post_id,
            {"mode": "MATERIAL_RELATED", "maxKeywords": 16, "excludeKeywords": []},
        )
    )
    await asyncio.sleep(0)
    gate.set()

    result = await asking
    await service._jobs.drain()

    assert result.trend_keywords
    # 최신순 1 + 소재 관련순 1. 화면의 요청이 두 번째 소재 관련순 수집을 만들지 않았다.
    assert provider.calls.count("MATERIAL_RELATED") == 1


async def test_다른_후보_보기는_선행분에_붙지_않는다():
    """'다른 후보 보기'는 일부러 다시 도는 것이다 — 붙으면 같은 목록을 다시 보게 된다."""
    provider = CountingTrendProvider()
    repository, service = build_prefetch_service(provider)
    task = build_task()
    await repository.create(task)

    service.start_keyword_prefetch(task)
    await service._jobs.drain()
    provider.calls.clear()

    await service.recommend_topics(
        task.post_id,
        {
            "mode": "MATERIAL_RELATED",
            "maxKeywords": 16,
            "excludeKeywords": [],
            "cursor": "next-page",
        },
    )

    assert provider.calls == ["MATERIAL_RELATED"]


async def test_선행_수집이_실패해도_화면은_새로_모은다():
    """가속 장치 하나가 제목 단계를 막지 않는다."""

    class 실패하는_수집기(CountingTrendProvider):
        async def fetch_trends(self, trend_input):
            self.calls.append(trend_input.mode.value)
            if len(self.calls) <= 2:
                raise RuntimeError("수집 실패")
            return TrendFetchResult(
                collected_at=NOW,
                mode=trend_input.mode,
                trend_keywords=[keyword("trend_1", "AI 검색")],
            )

    provider = 실패하는_수집기()
    repository, service = build_prefetch_service(provider)
    task = build_task()
    await repository.create(task)

    service.start_keyword_prefetch(task)
    await service._jobs.drain()

    result = await service.recommend_topics(
        task.post_id,
        {"mode": "MATERIAL_RELATED", "maxKeywords": 16, "excludeKeywords": []},
    )

    assert result.trend_keywords


# --------------------------------------------------- 소재 풀 조기 데우기


class InputCapturingProvider(CountingTrendProvider):
    """어떤 입력으로 수집했는지까지 기록한다."""

    def __init__(self, gate: "asyncio.Event | None" = None):
        super().__init__(gate)
        self.inputs: list = []

    async def fetch_trends(self, trend_input):
        self.inputs.append(trend_input)
        return await super().fetch_trends(trend_input)


async def test_소재만_정해져도_소재_풀을_미리_데운다():
    """2026-08-10 사용자 요청 — 소재를 적고 글 목적·연령·참고 자료를 만지기 시작한
    순간이면, 글(post)이 만들어지기 전이라도 소재 관련 수집을 시작할 수 있다."""
    provider = InputCapturingProvider()
    _, service = build_prefetch_service(provider)

    started = service.start_material_pool_warmup(
        "user_1", {"topic": "배틀그라운드", "purpose": ["정보 전달"]}
    )
    await service._jobs.drain()

    assert started is True
    assert provider.calls == ["MATERIAL_RELATED"]
    sent = provider.inputs[0]
    assert sent.input.topic == "배틀그라운드"
    assert sent.input.purpose == ["정보 전달"]
    # 노출 이력 키(user:post:mode)가 실제 글 화면과 겹치지 않는 합성 id를 쓴다.
    assert sent.post_id.startswith("warmup_")


async def test_같은_소재의_데우기는_겹쳐_돌지_않는다():
    """화면이 실수로 두 번 보내도(표기 차이 포함) 수집·판정 비용은 한 번이다."""
    gate = asyncio.Event()
    provider = CountingTrendProvider(gate)
    _, service = build_prefetch_service(provider)

    first = service.start_material_pool_warmup("user_1", {"topic": "배틀그라운드"})
    second = service.start_material_pool_warmup("user_1", {"topic": " 배틀 그라운드 "})
    gate.set()
    await service._jobs.drain()

    assert first is True and second is True
    assert provider.calls == ["MATERIAL_RELATED"]


async def test_소재가_없거나_몸통이_틀리면_데우지_않는다():
    provider = CountingTrendProvider()
    _, service = build_prefetch_service(provider)

    assert service.start_material_pool_warmup("user_1", {"topic": "   "}) is False
    assert service.start_material_pool_warmup("user_1", ["잘못된", "몸통"]) is False
    await service._jobs.drain()

    assert provider.calls == []
