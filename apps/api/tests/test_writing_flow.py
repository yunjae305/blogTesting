"""소재 입력부터 저장까지, 사용자가 실제로 지나는 길을 그대로 지난다.

2026-08-05 미팅 점검표의 두 항목을 자동으로 확인한다.

- "3개 방향을 모두 선택해 다음 단계로 정상 이동하고 오류 없이 결과가 생성된다"
- "소재 입력부터 미리보기·저장까지 모든 단계가 중단 없이 완료된다"

단위 테스트는 각 단계를 따로 본다. 그래서 단계 **사이**의 상태 전이가 어긋나는 문제는
아무도 잡지 못했다 — 이 파일은 그 이음매만 본다. 모델은 전부 스텁이라 과금이 없다.
"""

from datetime import datetime, timezone

import pytest

from app.errors import BlogTaskError
from app.llm import TopicRecommendationResult, TrendFetchResult
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.blog_task.service import BlogTaskService
from app.modules.draft.service import DraftService
from app.modules.persona import InMemoryPersonaRepository, PersonaService
from app.modules.trend.service import TrendService
from app.posting import PublishResult
from app.shared import (
    BlogTaskStatus,
    DraftGenerationResult,
    FinalPost,
    IntentCandidate,
    IntentValidationResult,
    PostingResultStatus,
    SearchSource,
    TopicCandidate,
    TrendKeyword,
    TrendSource,
)

NOW = "1970-01-01T00:00:00.000Z"

INPUT_BODY = {
    "userId": "user_1",
    "topic": "아이오나",
    "subject": "AI 블로그 자동화 서비스",
    "purpose": ["정보 전달"],
    "keywords": ["정보 전달"],
    "readerAgeRange": "20s",
    # 이름이 겹치는 소재에서 어느 대상인지 못박는 것은 사용자가 준 주소다.
    "referenceMaterials": [
        {"type": "URL", "value": "https://aiona.kr/"},
        {"type": "URL", "value": "https://aiona.kr/pricing"},
        {"type": "TEXT", "value": "요금제는 무료 체험 후 월 구독으로 전환된다."},
    ],
}

BODY_TEXT = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    for n in range(1, 25)
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ThreeIntentAnalyzer:
    """M3 스텁. 방향 후보 셋을 서로 다른 자료와 함께 돌려준다."""

    async def search_and_analyze(self, analysis_input, on_collected=None):
        if on_collected:
            await on_collected()
        return IntentValidationResult(
            prompt_version=analysis_input.prompt_version,
            provider="stub",
            model="stub",
            analyzed_at=_now(),
            intent_candidates=[
                IntentCandidate(
                    intent_id=f"{analysis_input.post_id}_intent_{n}",
                    title=f"방향 {n}",
                    target_reader=f"독자 {n}",
                    rationale=f"근거 {n}",
                    keywords=analysis_input.input.keywords,
                    sources=[
                        SearchSource(
                            title=f"자료 {n}",
                            url=f"https://aiona.kr/doc-{n}",
                            snippet=f"자료 {n} 요약",
                        )
                    ],
                )
                for n in (1, 2, 3)
            ],
        )


class StubTrendProvider:
    async def fetch_trends(self, trend_input):
        return TrendFetchResult(
            collected_at=NOW,
            mode=trend_input.mode,
            trend_keywords=[
                TrendKeyword(
                    trend_keyword_id="trend_1",
                    keyword="AI 자동화",
                    source=TrendSource.GOOGLE_TRENDS,
                    rank=1,
                    score=90,
                    collected_at=NOW,
                )
            ],
        )


class StubTopicGenerator:
    async def generate_topics(self, topic_input):
        return TopicRecommendationResult(
            generated_at=NOW,
            provider="stub",
            model="stub",
            topic_candidates=[
                TopicCandidate(
                    topic_candidate_id="topic_1",
                    title="아이오나로 블로그를 자동화하는 법",
                    description="정보형",
                    trend_keyword_ids=["trend_1"],
                    recommended=True,
                )
            ],
        )


class RecordingDraftGenerator:
    """어떤 방향으로 원고를 요청받았는지 기록한다."""

    def __init__(self):
        self.intents: list[str] = []

    async def generate_draft(self, draft_input):
        self.intents.append(draft_input.selected_intent.intent_id)
        return DraftGenerationResult(
            prompt_version="m4-draft@v1.0",
            provider="stub",
            model="stub",
            generated_at=NOW,
            final_post=FinalPost(
                title=f"완성 원고 ({draft_input.selected_intent.title})",
                body=BODY_TEXT,
                hashtags=["a", "b", "c", "d", "e"],
                html_content=f"<article><h1>완성 원고</h1><p>{BODY_TEXT}</p></article>",
                markdown_content=f"# 완성 원고\n\n{BODY_TEXT}",
            ),
        )


class SucceedingPostingWorker:
    async def publish(self, job):
        return PublishResult(result=PostingResultStatus.SUCCESS, post_url=None)


def build_services(draft_generator=None):
    repository = InMemoryBlogTaskRepository()
    personas = PersonaService(InMemoryPersonaRepository())
    blog_tasks = BlogTaskService(
        repository=repository,
        posting_worker=SucceedingPostingWorker(),
        web_search_analyzer=ThreeIntentAnalyzer(),
    )
    trends = TrendService(
        repository=repository,
        trend_provider=StubTrendProvider(),
        topic_generator=StubTopicGenerator(),
        persona_service=personas,
    )
    drafts = DraftService(
        repository=repository,
        draft_generator=draft_generator or RecordingDraftGenerator(),
        post_image_generator=None,
        user_settings_service=None,
        persona_service=personas,
    )
    return repository, blog_tasks, trends, drafts


async def _walk_to_intent_candidates(blog_tasks, trends):
    """소재 입력 → 트렌드 추천 → 제목 선택 → 검증까지. 방향 후보를 돌려준다."""
    task = await blog_tasks.create_blog_task(INPUT_BODY)
    assert task.status == BlogTaskStatus.REFERENCE_PROCESSING

    recommendation = await trends.recommend_topics(task.post_id, {"maxKeywords": 3})
    assert recommendation.trend_keywords

    topics = await trends.generate_topics(
        task.post_id, {"trendKeywordId": "trend_1", "keyword": "AI 자동화"}
    )
    assert topics.topic_candidates

    chosen = topics.topic_candidates[0]
    selected = await trends.select_topic(
        task.post_id,
        {
            "topicCandidateId": chosen.topic_candidate_id,
            "finalTopic": chosen.title,
            "selectedTrendKeywordIds": ["trend_1"],
            "selectedKeywords": ["AI 자동화"],
            "skipped": False,
        },
    )
    assert selected.status == BlogTaskStatus.SEARCH_ANALYZING

    analyzed = await blog_tasks.analyze_intent_candidates(task.post_id)
    candidates = analyzed.intent_validation_result.intent_candidates
    assert len(candidates) == 3
    return task.post_id, candidates


@pytest.mark.parametrize("choice", [0, 1, 2])
async def test_every_direction_can_be_chosen_and_produces_a_draft(choice):
    """점검표 1번. 세 방향 중 **무엇을 골라도** 다음 단계로 넘어가고 원고가 나온다.

    방향마다 따로 돌린다 — 하나로 묶으면 두 번째부터 실패해도 첫 성공에 가려진다.
    """
    generator = RecordingDraftGenerator()
    _repository, blog_tasks, trends, drafts = build_services(generator)
    post_id, candidates = await _walk_to_intent_candidates(blog_tasks, trends)
    picked = candidates[choice]

    selected = await blog_tasks.select_intent(post_id, {"intentId": picked.intent_id})

    assert selected.status == BlogTaskStatus.INTENT_SELECTED
    assert selected.selected_intent.intent_id == picked.intent_id
    # 점검표 2번: 고른 방향이 그대로 원고 생성으로 넘어가야 한다.
    assert selected.selected_intent.title == picked.title
    assert selected.selected_intent.target_reader == picked.target_reader

    generated = await drafts.generate_draft(post_id, {})

    assert generated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert generated.final_post.title.endswith(f"({picked.title})")
    # 원고 생성이 실제로 그 방향을 받았다 — 첫 후보로 조용히 바뀌지 않았다.
    assert generator.intents == [picked.intent_id]


async def test_excluding_sources_still_lets_every_direction_through():
    """검증 팝업에서 자료를 체크 해제한 채 방향을 골라도 막히지 않는다."""
    _repository, blog_tasks, trends, drafts = build_services()
    post_id, candidates = await _walk_to_intent_candidates(blog_tasks, trends)
    picked = candidates[2]

    selected = await blog_tasks.select_intent(
        post_id,
        {
            "intentId": picked.intent_id,
            "excludedSourceUrls": [source.url for source in picked.sources],
        },
    )

    assert selected.selected_intent.intent_id == picked.intent_id
    assert selected.selected_intent.sources == []

    generated = await drafts.generate_draft(post_id, {})
    assert generated.status == BlogTaskStatus.READY_TO_PUBLISH


async def test_choosing_a_direction_that_no_longer_exists_is_refused_clearly():
    """다시 검증으로 후보가 갈린 뒤 옛 방향을 제출한 경우. 조용히 첫 후보로 바꾸지 않고
    거절해야 화면이 다시 고르라고 안내할 수 있다."""
    _repository, blog_tasks, trends, _drafts = build_services()
    post_id, _candidates = await _walk_to_intent_candidates(blog_tasks, trends)

    with pytest.raises(BlogTaskError) as error:
        await blog_tasks.select_intent(post_id, {"intentId": "post_gone_intent_9"})

    assert error.value.code == "VALIDATION_FAILED"


async def test_the_whole_flow_runs_from_topic_input_to_a_saved_draft():
    """점검표 11번. 소재 입력 → 트렌드 → 제목 → 검증 → 방향 → 원고 → 저장 → 수정까지."""
    generator = RecordingDraftGenerator()
    repository, blog_tasks, trends, drafts = build_services(generator)
    post_id, candidates = await _walk_to_intent_candidates(blog_tasks, trends)

    await blog_tasks.select_intent(post_id, {"intentId": candidates[1].intent_id})
    await drafts.generate_draft(post_id, {})

    saved = await repository.find_by_post_id(post_id)
    assert saved.status == BlogTaskStatus.READY_TO_PUBLISH
    assert saved.final_post is not None
    assert saved.final_post.html_content
    # 사용자가 준 입력이 끝까지 살아 있다.
    assert saved.input.topic == "아이오나"
    assert saved.input.reader_age_range == "20s"
    assert len([m for m in saved.input.reference_materials if m.type == "URL"]) == 2
    assert saved.trend_selection.final_topic == "아이오나로 블로그를 자동화하는 법"

    # 저장된 원고를 사용자가 고쳐 다시 저장하는 것까지가 이 단계의 끝이다.
    edited = await drafts.update_draft_text(
        post_id, {"title": "직접 고친 제목", "html": "<p>직접 고친 본문입니다.</p>"}
    )
    assert edited.final_post.title == "직접 고친 제목"
    assert "직접 고친 본문" in edited.final_post.body
