"""글 맨 마지막에 붙는 마무리 블록(2026-08-19 사용자 지시).

본문은 광고가 아니어야 한다(``prompts.brand_utility_rules``). 그런데 글의 끝에는
"여기서 보면 된다"는 자리가 하나 있어야 하고, 사용자가 실제로 쓰던 글들이 전부 그
모양이었다 — 본문은 담담하고, 끝에 마스코트 한 장과 사실 한 줄, 그리고 링크.

둘은 충돌하지 않는다. 오히려 **본문에서 권유하지 않기 때문에** 마지막 한 줄이 신뢰를
얻는다. 그래서 규칙을 자리별로 나눈다: 본문에는 권유 문장 금지, 마지막 블록은 고정.

## 왜 모델이 아니라 코드가 붙이는가

1. **매번 똑같아야 하는 글자다.** 모델에게 맡기면 회차마다 문구가 흔들리고, 크레딧
   수·조건 같은 사실이 슬쩍 바뀐다("웰컴 크레딧 200" 같은 것이 나온다). 이 블록은
   사실을 적는 자리라 흔들리면 안 된다.
2. **붙이는 자리가 최종 검수 뒤다.** 검수 앞에 두면 검수가 이 블록을 광고 문구로 읽고
   지적하거나 고쳐 버린다 — 본문에 권유를 금지해 두었기 때문에 더욱 그렇다.

## 이미지

마스코트는 브랜드에 등록해 둔 이미지 중 하나다. 여기서 다시 들고 있지 않고 **글의
참고자료에서 찾아 쓴다**(브랜드 이미지는 이미 ``origin="brand"``로 펼쳐져 들어가 있다).
한 벌 더 저장하면 같은 base64가 글 문서마다 두 번씩 들어간다.

사용자가 올린 사진에 도는 개인정보 가림은 여기 적용하지 않는다. 이것은 사용자가 찍어
올린 사진이 아니라 브랜드가 자기 자산으로 등록한 그림이고, 가릴 개인정보가 있다면 그것은
브랜드 자료를 고칠 일이다.
"""

from app.shared import BRAND_MATERIAL_ORIGIN, BrandClosing, FinalPost
from app.shared.format import escape_html, now_iso
from app.shared.draft import GeneratedPostImage

from .images import dedupe_images, image_html, image_markdown, mime_type_from_data_url

#: 마무리 블록의 class 이름. 자식 요소들이 이것을 접두어로 쓴다(blog-closing-note 등).
CLOSING_CLASS = "blog-closing"

#: 블록이 **이미 붙어 있는지** 알아보는 표시. class 이름과 따로 두는 이유는, class가
#: 자식 요소마다 접두어로 들어가 여러 번 나타나기 때문이다 — 세는 자리에서 쓸 수 없다.
#: 이 속성은 블록 하나에 정확히 한 번이다.
CLOSING_MARKER = f'data-block="{CLOSING_CLASS}"'

#: 본문과 마무리 사이의 가로줄. 사용자가 쓰던 글에 있던 것으로, 여기서부터는 글이 아니라
#: 안내라는 것을 눈으로 알린다.
_DIVIDER_HTML = '<hr class="blog-closing-rule" />'
_DIVIDER_MARKDOWN = "---"


def _mascot(closing: BrandClosing, materials) -> GeneratedPostImage | None:
    """마무리에 붙일 브랜드 이미지. 이름이 맞는 것이 없으면 None(글자만 붙는다).

    브랜드가 넣어 준 자료 안에서만 찾는다 — 사용자가 올린 사진이 이름이 같다는 이유로
    마무리에 실리면 안 된다.
    """
    label = (closing.image_label or "").strip()
    if not label:
        return None
    for material in materials or []:
        if getattr(material, "origin", None) != BRAND_MATERIAL_ORIGIN:
            continue
        if getattr(material.type, "value", material.type) != "IMAGE":
            continue
        if (material.name or "").strip() != label:
            continue
        data_url = (material.value or "").strip()
        if not data_url.startswith("data:image/"):
            continue
        return GeneratedPostImage(
            data_url=data_url,
            alt_text=label,
            prompt="Brand closing image.",
            provider="brand",
            model="brand-image",
            generated_at=now_iso(),
            mime_type=mime_type_from_data_url(data_url),
            source="reference",
            media_kind="reference",
        )
    return None


def closing_html(closing: BrandClosing, mascot: GeneratedPostImage | None) -> str:
    link = (
        f'<a href="{escape_html(closing.url)}" rel="noopener">'
        f"{escape_html(closing.label)}</a>"
    )
    image = image_html(mascot) if mascot is not None else ""
    return (
        f'<section class="{CLOSING_CLASS}" {CLOSING_MARKER}>'
        f"{_DIVIDER_HTML}{image}"
        f'<p class="blog-closing-note">{escape_html(closing.note)}</p>'
        f'<p class="blog-closing-link">👉 {link}</p>'
        f"</section>"
    )


def closing_markdown(closing: BrandClosing, mascot: GeneratedPostImage | None) -> str:
    image = f"{image_markdown(mascot)}\n\n" if mascot is not None else ""
    return (
        f"{_DIVIDER_MARKDOWN}\n\n{image}{closing.note}\n\n"
        f"👉 [{closing.label}]({closing.url})"
    )


def closing_text(closing: BrandClosing) -> str:
    """``body``(순수 글자)에 붙는 모양. 링크는 주소를 그대로 적는다."""
    return f"{closing.note}\n\n👉 {closing.label} {closing.url}"


def has_closing(post: FinalPost) -> bool:
    """이미 붙어 있는가. 재시도·부분 재생성에서 두 번 붙는 것을 막는다."""
    return CLOSING_MARKER in (post.html_content or "")


def append_closing(
    post: FinalPost, closing: BrandClosing | None, materials=None
) -> FinalPost:
    """완성된 원고 맨 끝에 마무리를 붙인다.

    **최종 검수와 다듬기가 끝난 뒤에 부른다.** 그 앞에 두면 검수가 이 블록을 광고
    문구로 읽고 지적하거나 고쳐 버린다.

    붙일 것이 없거나 이미 붙어 있으면 원고를 그대로 돌려준다 — 부르는 쪽이 조건을
    따지지 않아도 되게 한다.
    """
    if closing is None or has_closing(post):
        return post

    mascot = _mascot(closing, materials)
    markdown = post.markdown_content or f"# {post.title}\n\n{post.body}"
    update = {
        "body": f"{(post.body or '').rstrip()}\n\n{closing_text(closing)}",
        "html_content": f"{(post.html_content or '').rstrip()}\n{closing_html(closing, mascot)}",
        "markdown_content": f"{markdown.rstrip()}\n\n{closing_markdown(closing, mascot)}",
    }
    if mascot is not None:
        # 이미지 목록에도 넣는다 — 발행 경로가 이 목록을 보고 data URL을 실제 주소로
        # 바꾼다(routes.get_post_image). 목록에 없으면 네이버에서 그림이 빠진다.
        update["images"] = dedupe_images([*(post.images or []), mascot])
    return post.model_copy(update=update)
