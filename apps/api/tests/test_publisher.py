"""test.ts.

Also covers the wrong-method rejection branches, which the original left
untested.
"""

import pytest

from app.posting import (
    ConnectedNaverPublisher,
    CopyPublisher,
    DefaultPostingWorker,
    PublishJob,
    UnimplementedAutoPublisher,
)
from app.shared import FinalPost, PostingMethod, PostingResultStatus

FINAL_POST = FinalPost(
    title="Final title",
    body="Final body",
    hashtags=["blogit"],
    html_content="<h1>Final title</h1><p>Final body</p>",
)


def build_job(method: PostingMethod) -> PublishJob:
    return PublishJob(post_id="post_1", user_id="user_1", method=method, final_post=FINAL_POST)


async def test_copy_publisher_marks_copy_publishing_successful():
    result = await CopyPublisher().publish(build_job(PostingMethod.COPY))

    assert result.result == PostingResultStatus.SUCCESS
    assert result.post_url is None


async def test_auto_publishing_says_it_did_not_publish():
    """It used to answer SUCCESS with a made-up blog.example.com URL, so the user saw
    발행 완료 and a link to nowhere. Nothing had been published."""
    result = await UnimplementedAutoPublisher().publish(build_job(PostingMethod.AUTO))

    assert result.result == PostingResultStatus.NEEDS_HUMAN
    assert result.post_url is None
    assert "자동 발행은 아직 준비 중" in result.error_message


async def test_default_posting_worker_routes_by_publishing_method():
    # 로컬의 실제 네이버 연결 여부와 무관한 순수 라우팅 테스트다.
    worker = DefaultPostingWorker(auto_publisher=UnimplementedAutoPublisher())

    copy = await worker.publish(build_job(PostingMethod.COPY))
    draft = await worker.publish(build_job(PostingMethod.DRAFT))
    auto = await worker.publish(build_job(PostingMethod.AUTO))

    assert copy.result == PostingResultStatus.SUCCESS
    assert draft.result == PostingResultStatus.FAIL
    assert auto.result == PostingResultStatus.NEEDS_HUMAN


async def test_connected_naver_publisher_reports_missing_connection(monkeypatch, tmp_path):
    import app.posting.config as config_module
    import app.posting.credentials as credentials_module

    monkeypatch.setattr(config_module, "naver_profile_dir", lambda _user_id=None: tmp_path)
    monkeypatch.setattr(credentials_module, "load_credentials", lambda _profile: None)
    monkeypatch.setattr(config_module, "naver_config_from_env", lambda **_kwargs: None)

    result = await ConnectedNaverPublisher().publish(build_job(PostingMethod.AUTO))

    assert result.result == PostingResultStatus.NEEDS_HUMAN
    assert "저장된 네이버 로그인 정보가 없습니다" in result.error_message


async def test_connected_naver_draft_reports_missing_connection(monkeypatch, tmp_path):
    import app.posting.config as config_module
    import app.posting.credentials as credentials_module

    monkeypatch.setattr(config_module, "naver_profile_dir", lambda _user_id=None: tmp_path)
    monkeypatch.setattr(credentials_module, "load_credentials", lambda _profile: None)
    monkeypatch.setattr(config_module, "naver_config_from_env", lambda **_kwargs: None)

    result = await ConnectedNaverPublisher().publish(build_job(PostingMethod.DRAFT))

    assert result.result == PostingResultStatus.NEEDS_HUMAN
    assert "저장된 네이버 로그인 정보가 없습니다" in result.error_message


@pytest.mark.parametrize(
    ("publisher", "wrong_method", "message"),
    [
        (CopyPublisher(), PostingMethod.AUTO, "CopyPublisher only supports copy publishing"),
        (
            UnimplementedAutoPublisher(),
            PostingMethod.COPY,
            "UnimplementedAutoPublisher only supports auto publishing",
        ),
    ],
)
async def test_publishers_reject_the_wrong_method(publisher, wrong_method, message):
    result = await publisher.publish(build_job(wrong_method))

    assert result.result == PostingResultStatus.FAIL
    assert result.error_message == message
