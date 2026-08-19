"""내 글 목록의 full 호환과 가벼운 summary 계약 회귀 테스트."""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import AuthService
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.blog_task.service import BlogTaskService
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    DraftGenerationResult,
    FinalPost,
    GeneratedPostImage,
    PostingLog,
    PostingMethod,
    PostingResultStatus,
    TrendSelection,
)


def _task(
    *,
    post_id: str,
    user_id: str,
    created_at: str,
    marker: str | None = None,
) -> BlogTask:
    final_post = None
    draft_result = None
    status = BlogTaskStatus.INPUT
    posting_logs = []
    if marker is not None:
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
            title="최종 제목",
            body=f"본문-{marker}",
            hashtags=["blogit"],
            images=[image],
            featured_image=image,
            html_content=f"<p>{marker}</p>",
            markdown_content=f"![image]({image.data_url})",
        )
        draft_result = DraftGenerationResult(
            prompt_version="m4-draft@test",
            provider="mock",
            model="mock-draft",
            generated_at=created_at,
            final_post=final_post,
        )
        status = BlogTaskStatus.READY_TO_PUBLISH
        posting_logs = [
            PostingLog(
                log_id=f"log_{post_id}",
                post_id=post_id,
                user_id=user_id,
                method=PostingMethod.AUTO,
                result=PostingResultStatus.SUCCESS,
                post_url=f"https://blog.example.com/{post_id}",
                created_at=created_at,
            )
        ]

    return BlogTask(
        post_id=post_id,
        user_id=user_id,
        status=status,
        version=3,
        created_at=created_at,
        updated_at=created_at,
        status_history=[],
        input=BlogTaskInput(
            topic=f"{post_id} 소재",
            subject="세부 주제",
            purpose=None,
            keywords=["정보 제공"],
            reference_materials=[],
        ),
        posting_logs=posting_logs,
        trend_selection=TrendSelection(
            final_topic="트렌드 제목",
            selected_trend_keyword_ids=[],
            skipped=True,
            selected_at=created_at,
        ),
        draft_generation_result=draft_result,
        final_post=final_post,
    )


async def _test_app():
    auth_service = AuthService(InMemoryUserRepository())
    writer = await auth_service.sign_up(
        {"email": "writer@example.com", "password": "password123", "nickname": "작성자"}
    )
    other = await auth_service.sign_up(
        {"email": "other@example.com", "password": "password123", "nickname": "다른 계정"}
    )

    repository = InMemoryBlogTaskRepository()
    marker = "SUMMARY_ROUTE_MUST_NOT_INCLUDE_" + "A" * 32768
    await repository.create(
        _task(
            post_id="post_old",
            user_id=writer.user.user_id,
            created_at="2026-01-01T00:00:00.000Z",
        )
    )
    await repository.create(
        _task(
            post_id="post_new",
            user_id=writer.user.user_id,
            created_at="2026-02-01T00:00:00.000Z",
            marker=marker,
        )
    )
    await repository.create(
        _task(
            post_id="post_other",
            user_id=other.user.user_id,
            created_at="2026-03-01T00:00:00.000Z",
            marker="OTHER_ACCOUNT_PAYLOAD",
        )
    )

    blog_task_service = BlogTaskService(repository, None, None)
    app = create_app()
    app.state.services = SimpleNamespace(
        auth_service=auth_service,
        blog_task_service=blog_task_service,
    )
    return app, writer.access_token, other.access_token, marker


def _authorization(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_posts_summary_is_small_owner_scoped_and_newest_first():
    app, writer_token, other_token, marker = await _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/posts?view=summary", headers=_authorization(writer_token))
        other_response = await client.get(
            "/posts?view=summary", headers=_authorization(other_token)
        )

    assert response.status_code == 200
    items = response.json()["data"]
    assert [item["postId"] for item in items] == ["post_new", "post_old"]
    assert items[0] == {
        "postId": "post_new",
        "userId": items[0]["userId"],
        "status": "READY_TO_PUBLISH",
        "version": 3,
        "createdAt": "2026-02-01T00:00:00.000Z",
        "updatedAt": "2026-02-01T00:00:00.000Z",
        "title": "최종 제목",
        "topic": "post_new 소재",
        "subject": "세부 주제",
        "purposes": ["정보 제공"],
        "postUrl": "https://blog.example.com/post_new",
        "hasFinalPost": True,
    }
    assert len(response.content) < 2_000
    encoded = response.text
    assert marker not in encoded
    for forbidden in (
        "dataUrl",
        "body",
        "htmlContent",
        "markdownContent",
        "draftGenerationResult",
    ):
        assert forbidden not in encoded

    assert other_response.status_code == 200
    assert [item["postId"] for item in other_response.json()["data"]] == ["post_other"]


async def test_posts_without_view_keeps_the_existing_full_response():
    app, writer_token, _other_token, marker = await _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/posts", headers=_authorization(writer_token))

    assert response.status_code == 200
    tasks = response.json()["data"]
    assert [task["postId"] for task in tasks] == ["post_new", "post_old"]
    assert tasks[0]["finalPost"]["body"].startswith("본문-")
    assert tasks[0]["draftGenerationResult"]["finalPost"]["title"] == "최종 제목"
    assert marker in response.text
    assert "title" not in tasks[0]


async def test_post_status_is_owner_scoped_and_never_returns_heavy_fields():
    app, writer_token, other_token, marker = await _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/posts/post_new/status", headers=_authorization(writer_token)
        )
        other_response = await client.get(
            "/posts/post_new/status", headers=_authorization(other_token)
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "postId": "post_new",
        "status": "READY_TO_PUBLISH",
        "version": 3,
        "hasIntentValidationResult": False,
        # 작업 현황 로그(2026-08-10). 생성이 돌지 않은 글은 빈 목록이다 — 여전히 가볍다.
        "activityLog": [],
    }
    assert len(response.content) < 500
    assert marker not in response.text
    for forbidden in (
        "input",
        "referenceMaterials",
        "dataUrl",
        "finalPost",
        "images",
        "featuredImage",
        "body",
        "htmlContent",
        "markdownContent",
        "draftGenerationResult",
    ):
        assert forbidden not in response.text

    # Deliberately do not reveal whether another account owns the post.
    assert other_response.status_code == 404
