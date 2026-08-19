"""M4 비평 → 통합 재작성에서 코드가 맡는 부분(critique.py).

모델 호출은 없다. 자리표 변환·복원·훼손 검사가 전부다 — 이 부분이 틀리면 멀쩡한
재작성이 버려지거나(과잉 거부), 이미지가 사라진 재작성이 통과한다(과소 거부).
"""

import pytest

from app.modules.draft.critique import (
    markdown_with_placeholders,
    placeholder_violation,
    rebuild_post,
)
from app.shared import FinalPost, GeneratedPostImage

NOW = "1970-01-01T00:00:00.000Z"


def image(n: int, caption: str | None = None) -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url=f"data:image/png;base64,IMG{n}",
        alt_text=f"사진{n}",
        prompt="a photo",
        provider="openai",
        model="gpt-image-2",
        generated_at=NOW,
        mime_type="image/png",
        source="generated",
        caption=caption,
    )


def post_with_images() -> FinalPost:
    markdown = (
        "# 제목\n\n"
        "![사진1](data:image/png;base64,IMG1)\n\n"
        "첫 문단입니다.\n\n"
        "![사진2](data:image/png;base64,IMG2)\n*출처: 예시 통계(2026)*\n\n"
        "둘째 문단입니다."
    )
    return FinalPost(
        title="제목",
        body="첫 문단입니다.\n\n둘째 문단입니다.",
        hashtags=["태그"],
        html_content="<article><h1>제목</h1></article>",
        markdown_content=markdown,
        images=[image(1), image(2, caption="출처: 예시 통계(2026)")],
        featured_image=image(1),
    )


def test_이미지가_자리표로_바뀌고_데이터가_모델에_가지_않는다():
    model_md, blocks = markdown_with_placeholders(post_with_images())

    assert "data:image" not in model_md
    assert "[[IMAGE:1]]" in model_md and "[[IMAGE:2]]" in model_md
    assert sorted(blocks) == [1, 2]
    # 캡션은 자리표 안에 있다 — 모델이 출처를 '다듬을' 수 없다.
    assert "출처: 예시 통계" not in model_md
    assert "출처: 예시 통계" in blocks[2]


def test_그대로_돌려주면_세_벌이_원형으로_복원된다():
    post = post_with_images()
    model_md, blocks = markdown_with_placeholders(post)

    rebuilt = rebuild_post(post, model_md, blocks)

    assert rebuilt is not None
    assert "data:image/png;base64,IMG1" in rebuilt.markdown_content
    assert "출처: 예시 통계" in rebuilt.markdown_content
    assert rebuilt.html_content.count("<img") == 2
    assert "[[IMAGE" not in rebuilt.html_content
    assert "[[IMAGE" not in rebuilt.body and "data:image" not in rebuilt.body
    # 재작성이 손대지 않는 것들.
    assert rebuilt.title == post.title
    assert rebuilt.hashtags == post.hashtags
    assert rebuilt.images == post.images


def test_자리표를_옮긴_재작성은_이미지도_함께_움직인다():
    # 2차 검토의 배치 지적을 통합이 반영한 경우다 — 옮기는 것은 허용한다.
    post = post_with_images()
    model_md, blocks = markdown_with_placeholders(post)
    moved = model_md.replace("[[IMAGE:2]]\n\n", "").replace(
        "둘째 문단입니다.", "둘째 문단입니다.\n\n[[IMAGE:2]]"
    )

    rebuilt = rebuild_post(post, moved, blocks)

    assert rebuilt is not None
    # IMG2가 둘째 문단 **뒤**로 갔다.
    assert rebuilt.html_content.index("둘째 문단") < rebuilt.html_content.index("IMG2")


@pytest.mark.parametrize(
    "mutate, 사유",
    [
        (lambda md: md.replace("[[IMAGE:2]]", ""), "사라진"),
        (lambda md: md + "\n\n[[IMAGE:3]]", "지어낸"),
        (lambda md: md + "\n\n[[IMAGE:1]]", "중복"),
    ],
)
def test_자리표를_훼손한_재작성은_통째로_버린다(mutate, 사유):
    post = post_with_images()
    model_md, blocks = markdown_with_placeholders(post)

    assert rebuild_post(post, mutate(model_md), blocks) is None
    assert 사유 in (placeholder_violation(mutate(model_md), blocks) or "")


def test_느슨한_자리표_표기도_받아_준다():
    # 모델이 "[[ image : 1 ]]"처럼 돌려줘도 같은 자리표다 — 표기 때문에 버리지 않는다.
    post = post_with_images()
    model_md, blocks = markdown_with_placeholders(post)
    sloppy = model_md.replace("[[IMAGE:1]]", "[[ image : 1 ]]")

    rebuilt = rebuild_post(post, sloppy, blocks)

    assert rebuilt is not None
    assert rebuilt.html_content.count("<img") == 2


def test_이미지가_없는_원고는_그대로_지나간다():
    post = FinalPost(
        title="제목",
        body="본문",
        hashtags=[],
        html_content="<article/>",
        markdown_content="# 제목\n\n본문입니다.",
    )
    model_md, blocks = markdown_with_placeholders(post)

    assert blocks == {}
    rebuilt = rebuild_post(post, model_md + "\n\n덧붙인 문단.", blocks)
    assert rebuilt is not None and "덧붙인 문단" in rebuilt.body
