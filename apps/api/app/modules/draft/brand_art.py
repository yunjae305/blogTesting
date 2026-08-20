"""브랜드가 등록해 둔 삽화를 본문에 넣는다(2026-08-20 사용자 지시).

AIONA에는 상황별 마스코트 그림이 있다(기본·인사·안내·조사·결과 전달·자료 건네기·고민·
축하). 사용자가 실제로 쓰던 글은 원고 한 편에 **이미지나 GIF를 두 장쯤** 넣는다. 그
자리를 여기서 만든다.

## 왜 기존 이미지 경로를 쓰지 않는가

본문 사진에는 이미 두 갈래가 있다 — 모델이 만드는 그림(카드 계획)과 사용자가 올린 사진
(`_reference_images`). 브랜드 삽화는 둘 다 아니다.

- **만드는 그림이 아니다.** 마스코트는 이미 그려져 있고, 모델에게 다시 그리라고 하면
  전혀 다른 캐릭터가 나온다.
- **사용자가 올린 사진도 아니다.** 그쪽 경로는 개인정보 가림과 근거 판정(어떤 역할의
  자료인가)을 거친다. 브랜드가 자기 자산으로 등록한 그림에는 가릴 것도, 판정할 것도
  없다 — 그 검사를 태우면 판정이 없다는 이유로 조용히 빠진다.

그래서 마무리 블록(`closing.py`)과 같은 자리에서, 같은 방식으로 붙인다: **검수·다듬기가
끝난 뒤에 코드가.**

## 어느 그림을, 어디에

**고르는 것**은 글마다 돌아간다. 언제나 앞 두 장을 쓰면 모든 글이 같은 그림으로 나온다.
글 id로 시작 자리를 정해(`post_id`) 돌리므로, 같은 글은 몇 번을 다시 만들어도 같은
그림이고 다른 글은 다른 그림이 된다 — 무작위가 아니라 **결정적**이다.

**넣는 자리**는 소제목 아래다. 두 장을 글 앞뒤로 갈라 놓는다(첫 장은 앞쪽 소제목, 둘째
장은 뒤쪽 소제목). 문단 사이에 끼우지 않는 이유는 소제목이 글에서 유일하게 **의미가
바뀌는 자리**이기 때문이다 — 문단 중간에 그림이 들어가면 읽던 문장이 끊긴다.

마무리 블록에 쓰는 그림은 뺀다. 같은 마스코트가 본문에 둘, 맨 끝에 하나로 세 번 나오면
그림이 아니라 무늬가 된다.
"""

from app.shared import BRAND_MATERIAL_ORIGIN, BrandClosing, FinalPost
from app.shared.draft import GeneratedPostImage
from app.shared.format import now_iso

from .images import (
    dedupe_images,
    image_html,
    image_markdown,
    insert_after_heading_html,
    insert_after_heading_markdown,
    mime_type_from_data_url,
)

#: 원고 한 편에 넣을 브랜드 삽화 수. 사용자가 실제로 쓰던 글이 두 장쯤이다.
#:
#: 늘리기 전에 생각할 것: 이 그림은 **소재와 무관한 마스코트**다. 많아질수록 글이
#: 브랜드 홍보물로 읽히고, 본문에 브랜드를 20%만 두기로 한 배분과도 어긋난다.
BRAND_ART_COUNT = 2

#: 이미 붙었는지 알아보는 표시. 재시도·부분 재생성에서 두 번 붙는 것을 막는다.
BRAND_ART_MARKER = 'data-brand-art="1"'


def _brand_images(materials, exclude_label: str) -> list[tuple[str, str]]:
    """브랜드가 넣어 준 그림들 — (이름, data URL).

    **브랜드 자료 안에서만** 찾는다(`origin="brand"`). 사용자가 올린 사진은 자기 경로가
    따로 있고, 그쪽은 개인정보 가림을 거쳐야 한다.
    """
    found: list[tuple[str, str]] = []
    for material in materials or []:
        if getattr(material, "origin", None) != BRAND_MATERIAL_ORIGIN:
            continue
        if getattr(material.type, "value", material.type) != "IMAGE":
            continue
        name = (material.name or "").strip()
        value = (material.value or "").strip()
        if not value.startswith("data:image/"):
            continue
        # 마무리 블록이 쓰는 그림은 뺀다 — 같은 마스코트가 세 번 나오면 무늬가 된다.
        if exclude_label and name == exclude_label:
            continue
        found.append((name, value))
    return found


def _as_post_image(name: str, data_url: str) -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url=data_url,
        # 대체텍스트는 등록해 둔 이름이다. 마스코트의 상황(인사·조사·축하)이 곧 이름이라
        # 화면 낭독기에도 그대로 뜻이 된다.
        alt_text=name or "브랜드 이미지",
        prompt="Brand illustration reused in the article.",
        provider="brand",
        model="brand-image",
        generated_at=now_iso(),
        mime_type=mime_type_from_data_url(data_url),
        source="reference",
        media_kind="reference",
    )


def _rotation_start(post_id: str, total: int) -> int:
    """이 글이 몇 번째 그림부터 쓸까. 글 id로 정해 **같은 글은 언제나 같게** 만든다.

    파이썬의 `hash`는 프로세스마다 값이 달라(PYTHONHASHSEED) 재생성 때 그림이 바뀐다.
    글자 코드를 더하는 것으로 충분하다 — 고르게 흩어질 필요는 없고, 글마다 달라지고
    다시 만들어도 같기만 하면 된다.
    """
    if total <= 0:
        return 0
    return sum(ord(char) for char in post_id) % total


def _headings(markdown: str) -> int:
    """본문 소제목 수. 그림을 어디에 끼울지 정하는 눈금이다."""
    return sum(1 for line in markdown.splitlines() if line.startswith("## "))


def has_brand_art(post: FinalPost) -> bool:
    return BRAND_ART_MARKER in (post.html_content or "")


def insert_brand_art(
    post: FinalPost,
    materials=None,
    *,
    post_id: str = "",
    closing: BrandClosing | None = None,
    count: int = BRAND_ART_COUNT,
) -> FinalPost:
    """완성된 원고에 브랜드 삽화를 넣는다.

    **최종 검수와 다듬기가 끝난 뒤에 부른다**(마무리 블록과 같은 자리). 검수는 글이
    자료와 맞는 말을 하는지 보는 단계라, 소재와 무관한 마스코트가 그 앞에 있으면
    '이 글에 있을 이유가 없는 이미지'로 지적된다.

    넣을 그림이 없거나 이미 넣었으면 원고를 그대로 돌려준다.
    """
    if count <= 0 or has_brand_art(post):
        return post

    available = _brand_images(materials, (closing.image_label or "").strip() if closing else "")
    if not available:
        return post

    # **생성 이미지 수에 잡히지 않는다**(2026-08-20 사용자 확인). 표·그래프가 사진 개수
    # 정책과 따로 세어지는 것과 같다 — 이 그림은 모델이 만든 것이 아니라 브랜드가 늘
    # 들고 다니는 세간이라, 사진을 몇 장 넣을지 정하는 계산에 끼면 안 된다.
    #
    # 그래서 목록 상한(MAX_POST_IMAGES)에서도 빼지 않고 **얹는다.** 상한에 맞춰 자르면
    # 사진이 많은 글에서만 마스코트가 조용히 빠져, 글마다 있고 없고가 달라진다.
    picks = min(count, len(available))
    if picks <= 0:
        return post

    start = _rotation_start(post_id, len(available))
    chosen = [available[(start + step) % len(available)] for step in range(picks)]
    images = [_as_post_image(name, data_url) for name, data_url in chosen]

    markdown = post.markdown_content or f"# {post.title}\n\n{post.body}"
    html = post.html_content or ""
    total_headings = _headings(markdown)

    if total_headings:
        # 두 장을 앞뒤로 갈라 놓는다. 소제목이 넷이면 2번과 4번 아래가 된다.
        for index, image in enumerate(images):
            section = round(total_headings * (index + 1) / (len(images) + 1)) or 1
            markdown = insert_after_heading_markdown(markdown, section, image_markdown(image))
            html = insert_after_heading_html(html, section, image_html(image))
    else:
        # 소제목이 없는 글. 끼울 눈금이 없어도 **그림은 들어간다**(2026-08-20 사용자
        # 지시: 항상 두 장). 문단 중간을 짚을 수 없으니 글 끝에 이어 붙인다 — 마무리
        # 블록은 이 뒤에 붙으므로 순서는 본문 → 그림 → 안내가 된다.
        gap = "\n\n"
        markdown = markdown.rstrip() + gap + gap.join(
            image_markdown(image) for image in images
        )
        html = html.rstrip() + "".join(image_html(image) for image in images)

    return post.model_copy(
        update={
            # 붙었다는 표시. 두 번 붙는 것을 막고, 나중에 이 블록만 따로 다룰 손잡이다.
            "html_content": html.replace("<article", f"<article {BRAND_ART_MARKER}", 1)
            if "<article" in html
            else f'<div {BRAND_ART_MARKER}></div>{html}',
            "markdown_content": markdown,
            # 발행 경로가 이 목록을 보고 data URL을 실제 주소로 바꾼다. 목록에 없으면
            # 네이버에서 그림이 빠진다. 여기서 자르지 않는 이유는 위와 같다 — 이 두 장은
            # 생성 이미지 수와 따로 센다.
            "images": dedupe_images([*(post.images or []), *images]),
        }
    )
