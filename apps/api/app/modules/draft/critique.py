"""M4 마무리의 '비평 → 통합 재작성'에서 코드가 맡는 부분(2026-08-07).

이 모듈은 **모델을 부르지 않는다**. 하는 일은 셋이다.

1. 완성 원고의 마크다운에서 이미지(base64 data URL, 수 MB)를 짧은 자리표
   ``[[IMAGE:n]]``로 바꿔 모델에게 보여 줄 수 있는 크기로 만든다.
2. 모델이 돌려준 개선 원고에서 자리표가 **전부, 각각 한 번씩** 살아 있는지 검사한다.
   하나라도 사라지거나 늘어나면 재작성 전체를 버린다 — 이미지는 이미 만들어져 그 자리에
   걸려 있고, 자리표가 곧 그 그림의 자리다.
3. 검사를 통과한 마크다운으로 세 벌(body·html·markdown)을 다시 세운다. 생성 때와 같은
   변환기(markdown_to_html·markdown_for_storage·text_from_html)를 쓴다 — 재작성본만
   다른 경로로 만들면 두 원고가 다른 모양이 된다.

문장 자리만 바꾸던 예전 방식(final_review.apply_review)은 그대로 남아 있다 — 비평·통합을
지원하지 않는 어댑터(구형·테스트 스텁·평가 harness)는 계속 그 길로 간다.
"""

import logging
import re

from app.llm.markdown_html import markdown_for_storage, markdown_to_html
from app.llm.parsing import text_from_html
from app.shared import FinalPost

from .images import image_html

logger = logging.getLogger(__name__)

#: 마크다운 속 이미지 블록. ``image_markdown``이 만드는 모양 그대로다 —
#: ``![alt](data:...)`` 뒤에 캡션(기울임 한 줄)이 붙을 수 있다. 캡션까지 자리표에
#: 넣는 이유: 캡션에는 출처·기준시점이 실리는데, 모델이 그것을 '다듬으면' 출처가
#: 달라진다. 자리표 안에 있으면 건드릴 수 없다.
_IMAGE_BLOCK = re.compile(r"!\[[^\]\n]*\]\(data:[^)\s]+\)(?:\n\*[^\n]+\*)?")

#: 모델이 자리표를 느슨하게 돌려줘도 받아 준다([[ IMAGE : 1 ]] 등).
_PLACEHOLDER = re.compile(r"\[\[\s*IMAGE\s*:\s*(\d+)\s*\]\]", re.IGNORECASE)


def markdown_with_placeholders(post: FinalPost) -> tuple[str, dict[int, str]]:
    """(모델에게 보여 줄 마크다운, 자리표 번호 → 원본 이미지 블록).

    이미지가 하나도 없으면 마크다운 그대로와 빈 매핑이다.
    """
    source = post.markdown_content or f"# {post.title}\n\n{post.body}"
    blocks: dict[int, str] = {}

    def replace(match: re.Match) -> str:
        number = len(blocks) + 1
        blocks[number] = match.group(0)
        return f"[[IMAGE:{number}]]"

    return _IMAGE_BLOCK.sub(replace, source), blocks


def placeholder_violation(improved_markdown: str, blocks: dict[int, str]) -> str | None:
    """자리표가 훼손됐으면 그 사유(로그용), 멀쩡하면 None."""
    found = [int(number) for number in _PLACEHOLDER.findall(improved_markdown)]
    expected = sorted(blocks)
    if sorted(found) == expected:
        return None
    missing = [n for n in expected if n not in found]
    invented = sorted({n for n in found if n not in blocks})
    duplicated = sorted({n for n in found if found.count(n) > 1})
    parts = []
    if missing:
        parts.append(f"사라진 자리표 {missing}")
    if invented:
        parts.append(f"지어낸 자리표 {invented}")
    if duplicated:
        parts.append(f"중복된 자리표 {duplicated}")
    return ", ".join(parts) or "자리표 불일치"


def rebuild_post(
    post: FinalPost, improved_markdown: str, blocks: dict[int, str]
) -> FinalPost | None:
    """개선 마크다운으로 세 벌을 다시 세운다. 자리표가 훼손됐으면 None — 원본을 쓴다.

    이미지 목록·대표 이미지·해시태그·제목은 그대로다. 재작성이 손대는 것은 글뿐이다.
    """
    reason = placeholder_violation(improved_markdown, blocks)
    if reason is not None:
        logger.warning("재작성 폐기(자리표 훼손: %s) — 원본 원고를 그대로 씁니다", reason)
        return None

    # 자리표 표기를 정규형으로 통일한 뒤 바꿔 넣는다.
    normalized = _PLACEHOLDER.sub(lambda m: f"[[IMAGE:{int(m.group(1))}]]", improved_markdown)

    # markdown: 자리표 → 원본 이미지 블록(캡션 포함).
    markdown_full = normalized
    for number, block in blocks.items():
        markdown_full = markdown_full.replace(f"[[IMAGE:{number}]]", block, 1)

    # html: 자리표를 그대로 통과시킨 뒤(markdown_to_html의 _MARKER_ONLY 규칙)
    # 이미지 태그로 바꾼다. 캡션은 image_html이 그린다.
    images_by_number = _images_by_placeholder(post, blocks)
    html = markdown_to_html(post.title, normalized)
    for number in blocks:
        image = images_by_number.get(number)
        html = html.replace(f"[[IMAGE:{number}]]", image_html(image) if image else "", 1)

    # body: 텍스트에는 이미지가 없다 — html에서 유도하면 자리표도 그림도 남지 않는다.
    return post.model_copy(
        update={
            "body": text_from_html(html),
            "html_content": html,
            "markdown_content": markdown_for_storage(post.title, markdown_full),
        }
    )


def _images_by_placeholder(post: FinalPost, blocks: dict[int, str]):
    """자리표 번호 → GeneratedPostImage. 원본 블록의 data URL로 짝을 찾는다.

    대표 이미지가 본문 목록에 없을 수 있어 둘 다 뒤진다. 짝이 없으면(이론상 없지만
    구형 문서) 그 자리는 비운다 — 지어낸 이미지를 넣지 않는다.
    """
    candidates = list(post.images or [])
    if post.featured_image is not None:
        candidates.insert(0, post.featured_image)
    by_url = {image.data_url: image for image in candidates if image.data_url}

    mapped = {}
    for number, block in blocks.items():
        match = re.search(r"\((data:[^)\s]+)\)", block)
        mapped[number] = by_url.get(match.group(1)) if match else None
    return mapped
