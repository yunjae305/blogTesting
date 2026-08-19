"""repository.test.ts.

Runs against both repository implementations. The Mongo tests are skipped when
no server is reachable — but when one is, they are what proves the Mongo
optimistic-concurrency path works, which the TypeScript suite never covered.
"""

import json
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.errors import BlogTaskError
from app.modules.blog_task.repository import (
    BLOG_TASK_LIST_ITEM_PROJECTION,
    BLOG_TASK_STATUS_PROJECTION,
    InMemoryBlogTaskRepository,
    MongoBlogTaskRepository,
)
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    DraftGenerationResult,
    FinalPost,
    GeneratedPostImage,
    IntentCandidate,
    IntentValidationResult,
    PostingLog,
    PostingMethod,

    PostingResultStatus,
    TaskProgress,
    TrendSelection,
)

MONGO_TEST_URI = "mongodb://localhost:27017/blog_it_test"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_task(**overrides) -> BlogTask:
    now = _now()
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        status=BlogTaskStatus.INPUT,
        version=1,
        created_at=now,
        updated_at=now,
        status_history=[],
        input=BlogTaskInput(topic="t", keywords=["k"], reference_materials=[]),
        posting_logs=[],
    )
    return BlogTask(**{**defaults, **overrides})


def _intent_result() -> IntentValidationResult:
    return IntentValidationResult(
        prompt_version="m3-intent@v1.0",
        provider="mock",
        model="mock",
        analyzed_at=_now(),
        intent_candidates=[
            IntentCandidate(
                intent_id="intent_1",
                title="Intent",
                target_reader="Reader",
                rationale="Rationale",
                keywords=["k"],
                sources=[],
            )
        ],
    )


async def _mongo_repo():
    client = AsyncIOMotorClient(MONGO_TEST_URI, serverSelectionTimeoutMS=1500)
    try:
        await client.admin.command("ping")
    except Exception:
        client.close()
        pytest.skip("MongoDB is not reachable")
    db = client.get_default_database()
    await db["blogTask"].delete_many({})
    # Duplicate-postId rejection is enforced by the index, not by application
    # code — scripts/init-mongo.mjs creates it in real deployments.
    await db["blogTask"].create_index("postId", unique=True, name="uniq_postId")
    return MongoBlogTaskRepository(db), client


@pytest.fixture(params=["memory", "mongo"])
async def repo(request):
    """Every test below runs twice — once per repository implementation."""
    if request.param == "memory":
        yield InMemoryBlogTaskRepository()
        return

    repository, client = await _mongo_repo()
    try:
        yield repository
    finally:
        client.close()


async def test_create_then_find_by_post_id_returns_the_stored_task(repo):
    await repo.create(build_task())

    found = await repo.find_by_post_id("post_1")

    assert found.post_id == "post_1"


async def test_find_by_user_and_post_id_is_owner_scoped(repo):
    await repo.create(build_task())

    assert await repo.find_by_user_and_post_id("user_1", "post_1") is not None
    assert await repo.find_by_user_and_post_id("user_2", "post_1") is None


async def test_find_status_is_owner_scoped_and_returns_only_polling_fields(repo):
    progress = TaskProgress(
        phase="SEARCH",
        step=1,
        total_steps=2,
        label="searching",
        steps=["search", "analyze"],
        started_at=_now(),
        updated_at=_now(),
    )
    await repo.create(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING,
            version=7,
            progress=progress,
            intent_validation_result=_intent_result(),
        )
    )

    found = await repo.find_status_by_user_and_post_id("user_1", "post_1")

    assert found is not None
    assert found.to_wire() == {
        "postId": "post_1",
        "status": "SEARCH_ANALYZING",
        "version": 7,
        "progress": progress.to_wire(),
        "hasIntentValidationResult": True,
        # 작업 현황 로그(2026-08-10)는 repository가 아니라 서비스가 프로세스 메모리에서
        # 붙인다 — 저장소 계층의 응답에는 빈 목록이 맞다.
        "activityLog": [],
    }
    assert await repo.find_status_by_user_and_post_id("user_2", "post_1") is None


async def test_owns_post_is_owner_scoped(repo):
    """권한 확인 전용 경로. 두 구현이 `find_by_user_and_post_id`와 같은 답을 내야 한다.

    프로토콜 계약과 `_id`만 받는지는 `test_blog_task_storage.py`가 본다(그쪽이 서비스
    호출부까지 대조해 더 촘촘하다). 여기서 보는 것은 **진짜 motor를 태웠을 때도 답이
    같은가**다 — 그쪽은 가짜 컬렉션을 쓴다.
    """
    await repo.create(build_task())

    assert await repo.owns_post("user_1", "post_1") is True
    assert await repo.owns_post("user_2", "post_1") is False
    assert await repo.owns_post("user_1", "없는_글") is False


async def test_list_by_user_id_returns_only_that_users_posts(repo):
    await repo.create(build_task())
    await repo.create(build_task(post_id="post_2", user_id="user_2"))

    found = await repo.list_by_user_id("user_1")

    assert [task.post_id for task in found] == ["post_1"]


def _large_ready_task(
    *,
    post_id: str,
    user_id: str,
    created_at: str,
    purpose: list[str] | None,
    marker: str,
) -> BlogTask:
    image = GeneratedPostImage(
        data_url=f"data:image/png;base64,{marker}",
        alt_text="대표 이미지",
        prompt="prompt",
        provider="mock",
        model="mock-image",
        generated_at=created_at,
        mime_type="image/png",
    )
    final_post = FinalPost(
        title="완성 제목",
        body=f"본문-{marker}",
        hashtags=["blogit"],
        images=[image],
        featured_image=image,
        html_content=f"<p>{marker}</p>",
        markdown_content=f"![image]({image.data_url})",
    )
    result = DraftGenerationResult(
        prompt_version="m4-draft@test",
        provider="mock",
        model="mock-draft",
        generated_at=created_at,
        final_post=final_post,
    )
    return build_task(
        post_id=post_id,
        user_id=user_id,
        status=BlogTaskStatus.READY_TO_PUBLISH,
        created_at=created_at,
        updated_at=created_at,
        input=BlogTaskInput(
            topic="원래 소재",
            subject="세부 주제",
            purpose=purpose,
            keywords=["키워드 폴백"],
            reference_materials=[],
        ),
        trend_selection=TrendSelection(
            final_topic="트렌드 제목",
            selected_trend_keyword_ids=[],
            skipped=True,
            selected_at=created_at,
        ),
        draft_generation_result=result,
        final_post=final_post,
        posting_logs=[
            PostingLog(
                log_id=f"log_{post_id}",
                post_id=post_id,
                user_id=user_id,
                method=PostingMethod.AUTO,
                result=PostingResultStatus.SUCCESS,
                post_url=f"https://blog.example.com/{post_id}",
                created_at=created_at,
            )
        ],
    )


async def test_list_items_are_owner_scoped_sorted_and_exclude_full_post_payload(repo):
    marker = "SUMMARY_MUST_NOT_INCLUDE_" + "A" * 4096
    older = _large_ready_task(
        post_id="post_old",
        user_id="user_1",
        created_at="2026-01-01T00:00:00.000Z",
        purpose=None,
        marker=marker,
    )
    newer = build_task(
        post_id="post_new",
        user_id="user_1",
        created_at="2026-02-01T00:00:00.000Z",
        updated_at="2026-02-01T00:00:00.000Z",
        input=BlogTaskInput(
            topic="새 소재",
            purpose=[],
            keywords=["빈 목적이면 노출되면 안 됨"],
            reference_materials=[],
        ),
    )
    other_user = build_task(post_id="post_other", user_id="user_2")
    for task in (older, newer, other_user):
        await repo.create(task)

    items = await repo.list_items_by_user_id("user_1")

    assert [item.post_id for item in items] == ["post_new", "post_old"]
    assert items[0].purposes == []
    assert items[0].title == "새 소재"
    assert items[0].has_final_post is False
    assert items[1].title == "완성 제목"
    assert items[1].subject == "세부 주제"
    assert items[1].purposes == ["키워드 폴백"]
    assert items[1].post_url == "https://blog.example.com/post_old"
    assert items[1].has_final_post is True

    wire = items[1].to_wire()
    assert set(wire) == {
        "postId",
        "userId",
        "status",
        "version",
        "createdAt",
        "updatedAt",
        "title",
        "topic",
        "subject",
        "purposes",
        "postUrl",
        "hasFinalPost",
    }
    encoded = json.dumps(wire, ensure_ascii=False)
    assert marker not in encoded
    for forbidden in (
        "dataUrl",
        "body",
        "htmlContent",
        "markdownContent",
        "draftGenerationResult",
    ):
        assert forbidden not in encoded


async def test_list_item_title_falls_back_from_final_post_to_trend_then_topic(repo):
    created_at = "2026-01-01T00:00:00.000Z"
    trend_only = build_task(
        post_id="post_trend",
        input=BlogTaskInput(topic="원래 소재", keywords=[], reference_materials=[]),
        trend_selection=TrendSelection(
            final_topic="트렌드 제목",
            selected_trend_keyword_ids=[],
            skipped=True,
            selected_at=created_at,
        ),
    )
    topic_only = build_task(
        post_id="post_topic",
        input=BlogTaskInput(topic="원래 소재", keywords=[], reference_materials=[]),
    )
    await repo.create(trend_only)
    await repo.create(topic_only)

    items = await repo.list_items_by_user_id("user_1")
    titles = {item.post_id: item.title for item in items}

    assert titles == {"post_trend": "트렌드 제목", "post_topic": "원래 소재"}


class _CapturingCursor:
    def __init__(self, documents):
        self._documents = documents
        self.sort_args = None

    def sort(self, key, direction):
        self.sort_args = (key, direction)
        return self

    def __aiter__(self):
        async def iterate():
            for document in self._documents:
                yield document

        return iterate()


class _CapturingCollection:
    def __init__(self, documents):
        self.cursor = _CapturingCursor(documents)
        self.query = None
        self.projection = None

    def find(self, query, projection):
        self.query = query
        self.projection = projection
        return self.cursor


class _CapturingStatusCollection:
    def __init__(self, document):
        self.document = document
        self.query = None
        self.projection = None

    async def find_one(self, query, projection):
        self.query = query
        self.projection = projection
        return self.document


async def test_mongo_list_items_uses_only_the_lightweight_projection():
    document = _large_ready_task(
        post_id="post_1",
        user_id="user_1",
        created_at="2026-01-01T00:00:00.000Z",
        purpose=["정보 제공"],
        marker="HEAVY_PAYLOAD",
    ).to_wire()
    collection = _CapturingCollection([document])
    # 이미지는 옆 컬렉션(post_images)에 산다. 저장소가 둘 다 잡으므로 가짜에도 둔다.
    repository = MongoBlogTaskRepository(
        {"blogTask": collection, "post_images": _CapturingCollection([])}
    )

    items = await repository.list_items_by_user_id("user_1")

    assert collection.query == {"userId": "user_1"}
    assert collection.projection == BLOG_TASK_LIST_ITEM_PROJECTION
    assert collection.cursor.sort_args == ("createdAt", -1)
    assert "finalPost.title" in collection.projection
    assert not any(
        key.startswith("draftGenerationResult")
        or key in {
            "finalPost.body",
            "finalPost.htmlContent",
            "finalPost.markdownContent",
            "finalPost.images",
            "finalPost.featuredImage",
        }
        for key in collection.projection
    )
    assert items[0].to_wire()["title"] == "완성 제목"
    assert "HEAVY_PAYLOAD" not in json.dumps(items[0].to_wire(), ensure_ascii=False)


async def test_mongo_status_uses_only_the_polling_projection():
    document = _large_ready_task(
        post_id="post_1",
        user_id="user_1",
        created_at="2026-01-01T00:00:00.000Z",
        purpose=["information"],
        marker="STATUS_MUST_NOT_READ_THIS_HEAVY_PAYLOAD",
    ).to_wire()
    collection = _CapturingStatusCollection(document)
    repository = MongoBlogTaskRepository(
        {"blogTask": collection, "post_images": _CapturingCollection([])}
    )

    snapshot = await repository.find_status_by_user_and_post_id("user_1", "post_1")

    assert snapshot is not None
    assert collection.query == {"userId": "user_1", "postId": "post_1"}
    assert collection.projection == BLOG_TASK_STATUS_PROJECTION
    assert set(collection.projection) == {
        "_id",
        "postId",
        "status",
        "version",
        "progress",
        "intentValidationResult.analyzedAt",
    }
    assert not any(
        key.startswith("input")
        or key.startswith("finalPost")
        or key.startswith("draftGenerationResult")
        for key in collection.projection
    )
    assert "STATUS_MUST_NOT_READ_THIS_HEAVY_PAYLOAD" not in json.dumps(
        snapshot.to_wire(), ensure_ascii=False
    )


async def test_create_rejects_a_duplicate_post_id(repo):
    await repo.create(build_task())

    with pytest.raises(BlogTaskError):
        await repo.create(build_task())


async def test_delete_by_post_id_removes_the_task(repo):
    await repo.create(build_task())

    await repo.delete_by_post_id("post_1")

    assert await repo.find_by_post_id("post_1") is None


async def test_delete_by_post_id_rejects_an_unknown_post_id(repo):
    with pytest.raises(BlogTaskError):
        await repo.delete_by_post_id("missing")


async def test_delete_by_user_and_post_id_cannot_delete_another_users_post(repo):
    await repo.create(build_task())

    with pytest.raises(BlogTaskError) as error:
        await repo.delete_by_user_and_post_id("user_2", "post_1")

    assert error.value.code == "NOT_FOUND"
    assert await repo.find_by_post_id("post_1") is not None


async def test_transition_bumps_version_and_appends_history(repo):
    await repo.create(build_task())

    updated = await repo.transition_status(
        "post_1", BlogTaskStatus.REFERENCE_PROCESSING, "tester"
    )

    assert updated.status == BlogTaskStatus.REFERENCE_PROCESSING
    assert updated.version == 2
    assert len(updated.status_history) == 1
    entry = updated.status_history[0]
    assert entry.from_ == BlogTaskStatus.INPUT
    assert entry.to == BlogTaskStatus.REFERENCE_PROCESSING
    assert entry.at == updated.updated_at
    assert entry.by == "tester"


async def test_transition_rejects_an_invalid_transition(repo):
    await repo.create(build_task())

    with pytest.raises(BlogTaskError):
        await repo.transition_status("post_1", BlogTaskStatus.GENERATING, "tester")


async def test_transition_rejects_an_unknown_post_id(repo):
    with pytest.raises(BlogTaskError):
        await repo.transition_status("missing", BlogTaskStatus.REFERENCE_PROCESSING, "tester")


async def test_append_posting_log_stores_a_publishing_result(repo):
    await repo.create(build_task())

    updated = await repo.append_posting_log(
        "post_1",
        PostingLog(
            log_id="log_1",
            post_id="post_1",
            user_id="user_1",
            method=PostingMethod.COPY,
            result=PostingResultStatus.SUCCESS,
            created_at=_now(),
        ),
    )

    assert len(updated.posting_logs) == 1
    assert updated.posting_logs[0].method == PostingMethod.COPY


async def test_save_trend_selection_stores_it_and_advances(repo):
    await repo.create(build_task(status=BlogTaskStatus.REFERENCE_PROCESSING, version=2))

    updated = await repo.save_trend_selection(
        "post_1",
        TrendSelection(
            final_topic="selected topic",
            selected_trend_keyword_ids=["trend_1"],
            skipped=False,
            selected_at="1970-01-01T00:00:00.000Z",
        ),
        "tester",
    )

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert updated.version == 3
    assert updated.trend_selection.final_topic == "selected topic"


async def test_save_trend_selection_can_repick_and_drops_the_old_validation(repo):
    """제목 다시 고르기(SEARCH_ANALYZING 자기 간선). 옛 제목으로 만든 검증 결과는 새
    제목에 대해 아무것도 말해 주지 않으므로 함께 버린다."""
    await repo.create(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING,
            version=3,
            trend_selection=TrendSelection(
                final_topic="first title",
                selected_trend_keyword_ids=["trend_1"],
                skipped=False,
                selected_at="1970-01-01T00:00:00.000Z",
            ),
            intent_validation_result=_intent_result(),
        )
    )

    updated = await repo.save_trend_selection(
        "post_1",
        TrendSelection(
            final_topic="second title",
            selected_trend_keyword_ids=["trend_2"],
            skipped=False,
            selected_at="1970-01-01T00:00:00.000Z",
        ),
        "tester",
    )

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert updated.trend_selection.final_topic == "second title"
    assert updated.intent_validation_result is None


async def test_save_intent_validation_result_does_not_change_status(repo):
    await repo.create(build_task(status=BlogTaskStatus.SEARCH_ANALYZING))

    updated = await repo.save_intent_validation_result("post_1", _intent_result())

    assert updated.status == BlogTaskStatus.SEARCH_ANALYZING
    assert len(updated.intent_validation_result.intent_candidates) == 1


async def test_select_intent_stores_the_candidate_and_appends_history(repo):
    await repo.create(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING, intent_validation_result=_intent_result()
        )
    )

    selected = await repo.select_intent("post_1", "intent_1", "tester")

    assert selected.status == BlogTaskStatus.INTENT_SELECTED
    assert selected.selected_intent.title == "Intent"
    assert selected.status_history[-1].to == BlogTaskStatus.INTENT_SELECTED


async def test_select_intent_rejects_an_unknown_candidate(repo):
    await repo.create(
        build_task(
            status=BlogTaskStatus.SEARCH_ANALYZING, intent_validation_result=_intent_result()
        )
    )

    with pytest.raises(BlogTaskError):
        await repo.select_intent("post_1", "intent_nope", "tester")


async def test_save_draft_generation_result_stores_final_post_and_advances(repo):
    await repo.create(build_task(status=BlogTaskStatus.GENERATING, version=6))

    updated = await repo.save_draft_generation_result(
        "post_1",
        DraftGenerationResult(
            prompt_version="m4-draft@v1.0",
            provider="mock",
            model="mock-draft-generator",
            generated_at="1970-01-01T00:00:00.000Z",
            final_post=FinalPost(
                title="Draft",
                body="Body",
                hashtags=["AI"],
                html_content="<article>Body</article>",
            ),
        ),
        "tester",
    )

    assert updated.status == BlogTaskStatus.READY_TO_PUBLISH
    assert updated.final_post.title == "Draft"
    assert updated.version == 7


async def test_mongo_rejects_a_write_against_a_stale_version():
    """The optimistic-concurrency guard. Untested in the TypeScript suite."""
    repository, client = await _mongo_repo()
    try:
        await repository.create(build_task())
        stale = await repository.find_by_post_id("post_1")

        # Someone else advances the task; our `stale` handle now points at v1.
        await repository.transition_status(
            "post_1", BlogTaskStatus.REFERENCE_PROCESSING, "other"
        )

        with pytest.raises(BlogTaskError, match="was updated concurrently"):
            await repository._apply(stale, {"$set": {"updatedAt": _now()}, "$inc": {"version": 1}})
    finally:
        client.close()

