"""생성한 이미지를 원고 안에 배치한다.service.ts.

M4는 이미지를 넣고 싶은 자리를 `[[IMAGE: <영어 장면> | alt=<한국어 설명>]]`로 표시하도록
요청받는다. 영어 장면은 이미지 모델 프롬프트가 되고, alt는 독자·검색이 보는 대체
텍스트가 된다. 생성된 이미지는 각자의 태그가 있던 그 자리에 다시 채워진다. 그 프롬프트
변경 이전에 쓰인 원고에는 태그가 없으므로, 예전 방식 — 이미지를 문단 사이에 고르게
흩뿌리는 것 — 을 폴백으로 남겨 둔다.
"""

import re

from app.shared import GeneratedPostImage
from app.shared.format import escape_html

# 본문 사진 수는 콘텐츠 설계가 정하고(planned_photo_count) 여기서 다시 자르지 않는다.
# 예전에는 총 5장으로 잘랐는데, 설계가 사진 4장과 도표 2장을 계획한 긴 글에서는 뒤쪽
# 그림이 조용히 사라졌다 — 자리를 잡아 둔 [[IMAGE:]] 태그만 남고 그림이 없는 셈이다.
# 아래 값은 폭주 방지선이지 규격이 아니다.
MAX_POST_IMAGES = 12
# 사용자가 올린 참고 이미지는 생성 이미지 위에 얹히는 것이라 따로 제한한다 — 업로드가
# 많다고 글이 사진첩이 되면 안 된다.
MAX_REFERENCE_IMAGES = 2

IMAGE_TAG_PATTERN = re.compile(r"\[\[IMAGE:\s*([^\]]+?)\s*]]")

_IMAGE_DATA_URL = re.compile(r"^data:image/(?:png|jpe?g);base64,", re.IGNORECASE)
_MIME_FROM_DATA_URL = re.compile(r"^data:([^;,]+)[;,]", re.IGNORECASE)


def is_image_data_url(value: str) -> bool:
    return bool(_IMAGE_DATA_URL.match(value))


def mime_type_from_data_url(value: str) -> str:
    match = _MIME_FROM_DATA_URL.match(value)
    return match.group(1) if match else "image/jpeg"


# 원고 안의 이미지 종류. Blog-it 서비스 UI(Post-it 카드·버튼)와 **원고 콘텐츠**를 가르는
# 경계다 — 예전에는 미리보기 CSS가 모든 이미지에 노란 오프셋 그림자를 붙여, 생성된 블로그
# 사진과 도표까지 서비스 UI 부품처럼 보였다.
MEDIA_KINDS = ("cover", "photo", "reference", "visual", "screenshot")


def media_kind_of(image: GeneratedPostImage) -> str:
    """이 이미지의 종류. 명시값이 우선, 없으면 source로 유추한다(옛 저장 문서 호환)."""
    kind = (image.media_kind or "").strip().lower()
    if kind in MEDIA_KINDS:
        return kind
    if image.source == "rendered":
        return "visual"
    if image.source == "reference":
        return "reference"
    return "photo"


def media_classes(image: GeneratedPostImage) -> str:
    return f"blog-media blog-media--{media_kind_of(image)}"


def image_html(image: GeneratedPostImage) -> str:
    # 캡션은 <figcaption>이 아니라 별도 문단이다: figcaption은 TipTap 편집기와 서버
    # allowlist, 네이버 발행 분할 어디에서도 살아남지 못하지만 <p><em>은 전 경로에서
    # 보존된다.
    caption = (
        f'<p class="visual-caption"><em>{escape_html(image.caption)}</em></p>'
        if image.caption
        else ""
    )
    kind = media_kind_of(image)
    # class는 네이버로 복사될 때 걷힌다(utils.stripEditorAttributes). 그래도 이미지 자체는
    # 그대로 보이므로, 여기 class는 우리 화면의 표현을 정하는 용도로만 존재한다.
    return (
        f'<figure class="{media_classes(image)}" data-media-kind="{kind}">'
        f'<img src="{escape_html(image.data_url)}" '
        f'alt="{escape_html(image.alt_text)}" /></figure>{caption}'
    )


def image_markdown(image: GeneratedPostImage) -> str:
    caption = f"\n*{image.caption}*" if image.caption else ""
    return f"![{image.alt_text}]({image.data_url}){caption}"


# 태그 안의 alt 필드 구분자. 영어 장면(이미지 모델용)과 한국어 alt(독자·검색용)를 가른다.
_ALT_FIELD = re.compile(r"\|\s*alt\s*=\s*", re.IGNORECASE)


def extract_image_tags(content: str) -> list[tuple[str, str | None]]:
    """본문 태그를 (영어 장면 묘사, 한국어 alt)로 분리해 뽑는다.

    한국어 글의 alt에 영어 장면 묘사가 그대로 실리는 것을 막으려고 태그에 alt 필드를
    나눴다. alt가 없는 옛 태그는 (장면, None)으로 나온다 — 그때는 영어 장면이 alt 폴백.
    """
    tags: list[tuple[str, str | None]] = []
    for match in IMAGE_TAG_PATTERN.finditer(content):
        scene, *alt = _ALT_FIELD.split(match.group(1).strip(), maxsplit=1)
        tags.append((scene.strip(), alt[0].strip() if alt and alt[0].strip() else None))
    return tags


def dedupe_images(images: list[GeneratedPostImage]) -> list[GeneratedPostImage]:
    seen: set[str] = set()
    result = []
    for image in images:
        if image.data_url in seen:
            continue
        seen.add(image.data_url)
        result.append(image)
    return result


def _collapse_blank_lines(content: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def replace_image_tags(content: str, images: list[GeneratedPostImage], to_markup) -> str:
    """N번째 태그에 N번째 이미지를 채운다. 남는 태그는 버린다."""
    counter = iter(range(len(images)))

    def substitute(_match: re.Match) -> str:
        index = next(counter, None)
        return to_markup(images[index]) if index is not None else ""

    return _collapse_blank_lines(IMAGE_TAG_PATTERN.sub(substitute, content))


def strip_image_tags(content: str) -> str:
    return _collapse_blank_lines(IMAGE_TAG_PATTERN.sub("", content))



# 카드 배치용: N번째 소제목(섹션) 바로 아래에 넣는다. 콘텐츠 설계의 section-N이 원고의
# N번째 `##`/<h2>와 짝을 이룬다 — 설계 준수 규칙이 섹션 순서 유지를 요구하므로 순번
# 매칭이 소제목 문구 매칭(원고가 표현을 다듬을 수 있다)보다 안전하다.
_MARKDOWN_HEADING = re.compile(r"^##\s.+$", re.MULTILINE)
_HTML_HEADING_CLOSE = re.compile(r"</h2>", re.IGNORECASE)


def insert_after_heading_markdown(content: str, section_number: int, markup: str) -> str:
    """N번째 `##` 소제목 줄 바로 아래에 삽입한다. 소제목이 모자라면 끝에 붙인다."""
    matches = list(_MARKDOWN_HEADING.finditer(content))
    if section_number < 1 or section_number > len(matches):
        return f"{content}\n\n{markup}"
    end = matches[section_number - 1].end()
    return f"{content[:end]}\n\n{markup}{content[end:]}"


def insert_after_heading_html(content: str, section_number: int, markup: str) -> str:
    """N번째 </h2> 바로 뒤에 삽입한다. 소제목이 모자라면 끝에 붙인다."""
    matches = list(_HTML_HEADING_CLOSE.finditer(content))
    if section_number < 1 or section_number > len(matches):
        return f"{content}\n{markup}"
    end = matches[section_number - 1].end()
    return f"{content[:end]}\n{markup}{content[end:]}"


def _insert_after_paragraph(content: str, insertion: str, target: int) -> str:
    counter = {"n": 0}

    def substitute(match: re.Match) -> str:
        counter["n"] += 1
        return f"{match.group(0)}\n{insertion}" if counter["n"] == target else match.group(0)

    return re.sub(r"</p>", substitute, content, flags=re.IGNORECASE)


def insert_html_images(content: str, images: list[GeneratedPostImage]) -> str:
    new_images = [image for image in images if image.data_url not in content]
    if not new_images:
        return content

    figures = [image_html(image) for image in new_images]
    paragraph_count = len(re.findall(r"</p>", content, flags=re.IGNORECASE))
    if paragraph_count == 0:
        return "\n".join(figures) + "\n" + content

    result = content
    for index, figure in enumerate(figures):
        target = max(1, round(((index + 1) * paragraph_count) / (len(figures) + 1)))
        result = _insert_after_paragraph(result, figure, target)
    return result


def insert_markdown_images(content: str, images: list[GeneratedPostImage]) -> str:
    new_images = [image for image in images if image.data_url not in content]
    if not new_images:
        return content

    blocks = re.split(r"\n{2,}", content)
    if len(blocks) <= 1:
        return "\n\n".join(image_markdown(i) for i in new_images) + "\n\n" + content

    insertions: dict[int, list[str]] = {}
    for index, image in enumerate(new_images):
        target = max(0, round(((index + 1) * (len(blocks) - 1)) / (len(new_images) + 1)))
        insertions.setdefault(target, []).append(image_markdown(image))

    out: list[str] = []
    for index, block in enumerate(blocks):
        out.append(block)
        out.extend(insertions.get(index, []))
    return "\n\n".join(out)
