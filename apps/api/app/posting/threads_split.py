"""완성된 원고를 **그대로** 연속 스레드로 나눈다 — 요약하지 않는다.

## 왜 요약을 그만뒀나 (2026-08-04 사용자 결정)

처음에는 블로그 원고를 500자 하나로 요약해 올렸고, 그다음에는 LLM에게 "2~5개 스레드로
나눠 쓰라"고 시켰다. 둘 다 **다시 쓰는 것**이라 원고에 있던 문장이 사라졌다. 사용자가
원한 것은 그게 아니다 — "요약하지말고 생성된 원고 그대로 스레드에 게시".

그래서 여기서는 모델을 부르지 않는다. 원고를 문단 경계에서 잘라 스레드 한도(500자)에
채워 담을 뿐이다. 문장은 하나도 바뀌지 않고, 순서도 원고 그대로다.

## 이미지도 원고 자리 그대로

원고 마크다운에는 이미지가 `![alt](data:image/...)`로 박혀 있다. 그 이미지가 나온
자리에서 만들고 있던 스레드에 붙인다 — 표지 이미지는 첫 스레드다. 브라우저 발행기가
이 data URL을 임시 파일로 풀어 작성창에 올린다(threads_browser._attach_images).

## 자르는 규칙

한도를 넘기면 스레드가 글을 통째로 거절하므로 한도는 코드가 지킨다.

1. 문단(빈 줄) 경계에서 먼저 나눈다 — 문단은 쪼개지 않는다.
2. 문단 하나가 한도를 넘으면 문장 경계에서 나눈다.
3. 문장 하나가 한도를 넘으면(표·긴 인용) 그때만 글자로 자른다.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.shared import FinalPost

if TYPE_CHECKING:
    from .publisher import PublishJob

logger = logging.getLogger(__name__)

# 스레드 하나의 글자 한도(발행 계층의 기준값). llm 계층은 posting을 참조하지 않으려고
# 같은 값을 THREADS_POST_TEXT_LIMIT으로 따로 둔다 — 값이 어긋나면 테스트가 잡는다.
THREAD_TEXT_LIMIT = 500
# 스레드 하나에 붙일 수 있는 이미지 수. 실제 한도(20장)보다 낮게 잡은 안전선이다 —
# 한 스레드에 그림이 열 장 붙는 원고면 나눠 붙는 편이 읽기에도 낫다.
MAX_IMAGES_PER_THREAD = 8
# 폭주 방지선. 원고가 아무리 길어도 이보다 많은 스레드로 나누지 않는다.
MAX_THREADS = 25

# 원고 마크다운에 박힌 이미지. data URL만 관심 대상이다(외부 URL은 파일로 풀 수 없다).
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\(\s*(data:image/[^)\s]+)\s*\)")
# 그림이 채워지지 않고 남은 자리표시 태그. 글에 실을 것이 아니다.
# STICKER는 네이버 발행 전용 자리 표식(2026-08-10) — 스레드에는 스티커가 없다.
_MARKER_TAG = re.compile(r"\[\[(?:IMAGE|VISUAL|STICKER):[^\]]*\]\]", re.IGNORECASE)
_MIME_FROM_DATA_URL = re.compile(r"^data:([^;,]+)[;,]", re.IGNORECASE)

# --- 마크다운 장식 걷어내기 ---------------------------------------------------
#
# 스레드에는 소제목·굵게 같은 서식이 없다. 기호를 그대로 두면 `## 제목`·`**중요**`가
# 글자로 보인다. 낱말은 하나도 빼지 않고 **기호만** 없앤다.
# 표는 예외다 — 기호만 없애면 열이 뒤섞인 글자 무더기가 남으므로, 행마다 "머리글: 값"
# 목록으로 푼다(_convert_tables). 이때도 낱말은 그대로다(첫 열의 머리글만 빠진다).
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(\S(?:.*?\S)?)\1", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HORIZONTAL_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)

_SENTENCE_END = ("다. ", "다.\n", ". ", "! ", "? ", "요. ", "죠. ")

# 표의 머리글과 본문을 가르는 구분선 칸(`---`·`:---:`). 이 줄이 있어야 표로 인정한다 —
# 산문에 낀 `|`를 표로 오인해 문장을 재조립하는 것보다, 못 알아본 표가 그대로 남는 편이 낫다.
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


def _table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    cells = [cell for cell in _table_cells(line) if cell]
    return bool(cells) and all(_TABLE_SEPARATOR_CELL.match(cell) for cell in cells)


def _table_entry(headers: list[str], cells: list[str]) -> list[str]:
    """표의 한 행을 줄 목록으로 푼다. 첫 칸이 제목 줄, 나머지는 "머리글: 값"이다."""
    lines: list[str] = []
    for position, value in enumerate(cells):
        if not value:
            continue
        label = headers[position] if position < len(headers) else ""
        if not lines:
            lines.append(f"• {value}")
        elif label:
            lines.append(f"  {label}: {value}")
        else:
            lines.append(f"  {value}")
    return lines


def _convert_tables(text: str) -> str:
    """마크다운 표를 스레드에서 읽히는 목록으로 바꾼다.

    스레드는 순수 텍스트라 표를 렌더링하지 않는다 — 그대로 두면 `|`와 `---` 구분선이
    기호째 보인다(2026-08-04 사용자 보고). 셀의 낱말은 전부 살리고 배치만 바꾼다.
    """
    if "|" not in text:
        return text
    lines = text.splitlines()
    result: list[str] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        if not (
            _is_table_row(line)
            and position + 1 < len(lines)
            and _is_table_separator(lines[position + 1])
        ):
            result.append(line)
            position += 1
            continue
        headers = _table_cells(line)
        position += 2
        entries: list[str] = []
        while position < len(lines) and _is_table_row(lines[position]):
            if not _is_table_separator(lines[position]):
                entry = _table_entry(headers, _table_cells(lines[position]))
                if entry:
                    entries.append("\n".join(entry))
            position += 1
        if entries:
            result.append("\n\n".join(entries))
        else:
            # 본문 행이 없는 표 — 머리글의 낱말을 버리지 않고 한 줄로 남긴다.
            result.append("• " + " · ".join(header for header in headers if header))
    return "\n".join(result)


@dataclass(frozen=True)
class ThreadPiece:
    """스레드 하나에 올릴 것 — 글과, 그 자리에 있던 이미지들."""

    text: str
    # 원고에 박혀 있던 data URL 그대로다. 파일로 푸는 일은 발행기가 한다.
    images: tuple[str, ...] = field(default=())


def plain_text(block: str) -> str:
    """마크다운 장식을 걷어낸 글. 낱말은 그대로 둔다."""
    # 표가 먼저다 — 다른 치환이 칸 안의 기호를 건드리기 전에 행 구조를 읽어야 한다.
    text = _convert_tables(block)
    text = _MARKER_TAG.sub(" ", text)
    text = _IMAGE_MD.sub(" ", text)
    text = _HORIZONTAL_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    # 강조는 안쪽부터 여러 겹일 수 있다(`**_굵고 기울임_**`). 더 줄지 않을 때까지 벗긴다.
    for _ in range(3):
        stripped = _EMPHASIS.sub(r"\2", text)
        if stripped == text:
            break
        text = stripped
    text = _TRAILING_SPACES.sub("", text)
    # 문단 안의 줄바꿈은 살린다 — 원고의 줄 나눔이 스레드에서도 그대로 보여야 한다.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_to_limit(text: str, limit: int = THREAD_TEXT_LIMIT) -> list[str]:
    """한 덩어리를 한도 안의 조각들로 나눈다. 문장 경계 우선, 안 되면 글자로 자른다."""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # 못 찾은 표시의 rfind는 -1이다 — 그대로 len을 더하면 없는 경계가 이긴다.
        cut = -1
        for mark in _SENTENCE_END:
            found = window.rfind(mark)
            if found >= 0:
                cut = max(cut, found + len(mark))
        if cut < limit // 3:
            # 문장 경계가 너무 앞이면 통째로 앞쪽만 남는다 — 어절 경계로 물러선다.
            space = window.rfind(" ")
            cut = space if space >= limit // 3 else limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return [chunk for chunk in chunks if chunk]


def _units(post: FinalPost) -> list[tuple[str, str]]:
    """원고를 순서대로 늘어놓은 조각들. ``("text"|"image", 값)``.

    본문은 마크다운을 쓴다 — 이미지가 data URL로 박혀 있고 서식 기호를 걷어내기 쉽다.
    마크다운이 없는 옛 문서는 ``body``로 물러선다.
    """
    units: list[tuple[str, str]] = []
    seen_images: set[str] = set()

    title = plain_text(post.title or "")
    if title:
        units.append(("text", title))

    source = (post.markdown_content or "").strip() or (post.body or "").strip()
    blocks = re.split(r"\n{2,}", source)

    featured = getattr(post, "featured_image", None)
    if featured is not None and (featured.data_url or "").startswith("data:image/"):
        # 표지는 원고의 맨 앞이다 — 첫 스레드에 붙는다. 다만 본문 첫머리에 같은 그림이
        # 이미 박혀 있는 원고가 많다(실측 2026-08-04) — 그때 또 붙이면 같은 사진이 두 번
        # 올라간다. 본문에 있으면 여기서는 건너뛰고 제자리에서 붙게 둔다.
        if featured.data_url not in source:
            units.append(("image", featured.data_url))
            seen_images.add(featured.data_url)

    title_dropped = not title
    for block in blocks:
        urls = _IMAGE_MD.findall(block)
        for url in urls:
            if url in seen_images:
                continue
            seen_images.add(url)
            units.append(("image", url))

        text = plain_text(block)
        if not text:
            continue
        if urls:
            # 이미지 블록에 남은 글은 캡션이다(`![alt](…)\n*출처 : …*`). 사진과 함께
            # 보이라고 붙인 설명이라 사진 없이 글자만 남으면 뜬금없다 — 실제로 게시된
            # 글에 `출처 : "imgnews.naver.net"`만 덩그러니 남았다(2026-08-04).
            continue
        if not title_dropped and text == title:
            # 본문 첫 소제목이 제목과 같은 원고가 있다 — 그대로 두면 제목이 두 번 보인다
            # (네이버 발행도 같은 이유로 제목과 같은 <h1>을 지운다).
            title_dropped = True
            continue
        title_dropped = True
        units.append(("text", text))

    hashtags = " ".join(
        f"#{tag.lstrip('#').strip()}"
        for tag in (post.hashtags or [])
        if tag and tag.strip()
    )
    if hashtags:
        units.append(("text", hashtags))
    return units


def cover_image_of(post: FinalPost | None) -> str | None:
    """원고의 **표지 그림** data URL. 없으면 None.

    스레드 전용 원고(llm/threads_prompts.py)는 글을 새로 쓰므로 본문 속 그림이 어디에
    붙을지 알 수 없다. 그래도 사진이 하나도 없는 글은 스레드에서 눈에 띄지 않으므로,
    표지 한 장만 첫 스레드에 붙인다(2026-08-06). 표지가 따로 없으면 본문에 처음 나오는
    그림을 표지로 본다 — 원고 맨 앞의 그림이 대개 그 역할이다.
    """
    images = post_images_of(post)
    return images[0] if images else None


def post_images_of(post: FinalPost | None) -> list[str]:
    """원고에 실린 그림 전부(data URL, 원고 순서, 중복 제거). 표지가 맨 앞이다."""
    if post is None:
        return []
    images: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url.startswith("data:image/") and url not in seen:
            seen.add(url)
            images.append(url)

    featured = getattr(post, "featured_image", None)
    add((getattr(featured, "data_url", "") or "") if featured is not None else "")
    source = (post.markdown_content or "").strip() or (post.body or "").strip()
    for match in _IMAGE_MD.finditer(source):
        add(match.group(1))
    return images


def split_final_post(post: FinalPost) -> list[ThreadPiece]:
    """원고 하나를 연속 스레드로 나눈다. 글자는 바뀌지 않는다.

    빈 원고면 빈 목록이다 — 발행기가 "실을 텍스트가 없습니다"로 거절한다.
    """
    pieces: list[ThreadPiece] = []
    parts: list[str] = []
    images: list[str] = []
    used = 0

    def flush() -> None:
        nonlocal used
        text = "\n\n".join(parts).strip()
        if text or images:
            pieces.append(ThreadPiece(text=text, images=tuple(images)))
        parts.clear()
        images.clear()
        used = 0

    for kind, value in _units(post):
        if kind == "image":
            if len(images) >= MAX_IMAGES_PER_THREAD:
                flush()
            images.append(value)
            continue
        for chunk in split_to_limit(value):
            extra = len(chunk) + (2 if parts else 0)  # 문단 사이 빈 줄 몫
            if parts and used + extra > THREAD_TEXT_LIMIT:
                flush()
                extra = len(chunk)
            parts.append(chunk)
            used += extra
    flush()

    if len(pieces) > MAX_THREADS:
        logger.warning(
            "원고가 스레드 %d개로 나뉘어 %d개까지만 올립니다 — 원고가 비정상적으로 깁니다.",
            len(pieces),
            MAX_THREADS,
        )
        pieces = pieces[:MAX_THREADS]
    return pieces


def decode_data_url(value: str) -> tuple[bytes, str] | None:
    """data URL을 (바이트, 확장자)로 푼다. 풀 수 없으면 None.

    실패를 예외로 올리지 않는 이유: 그림 한 장이 깨졌다고 글 전체를 못 올리는 것보다,
    그 장만 빼고 올리며 경고를 남기는 편이 낫다(발행기가 그렇게 쓴다).
    """
    if not value.startswith("data:image/") or "," not in value:
        return None
    header, _, payload = value.partition(",")
    if ";base64" not in header.lower():
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    match = _MIME_FROM_DATA_URL.match(value)
    mime = (match.group(1) if match else "image/jpeg").lower()
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".jpg")
    return raw, suffix


def publish_pieces_for(job: PublishJob) -> list[ThreadPiece]:
    """이 발행이 스레드에 실을 조각들. **순서가 곧 게시 순서**다.

    (2026-08-10 posting.threads에서 옮겨 왔다 — 공식 API 발행기를 걷어내면서, 브라우저
    발행기가 쓰는 이 선택 규칙만 분할 규칙과 같은 자리에 남겼다.)

    ## 두 갈래

    - ``job.threads_texts``가 있으면 **그것이 게시물이다.** 스레드 전용 생성기가 소재를
      스레드 문법으로 새로 쓴 글이다(modules/blog_task/service._write_threads_post).
      2026-08-06 사용자 요청으로 되살린 경로이고, 이것이 기본이다.
    - 없으면 블로그 원고를 문단 경계에서 잘라 담는다(split_final_post). 생성기가 없는
      구성(테스트·구형 조립)의 **폴백**이다.

    ## 이 자리의 내력

    2026-08-04에 "요약하지말고 생성된 원고 그대로"라는 지시로 생성 경로를 걷어냈다가,
    2026-08-06에 "500자로 자르지 말고 쓰레드 단일 글 생성 방식으로"라는 지시로 되살렸다.
    두 방식이 다 남아 있는 이유는 폴백이 필요해서다 — 어느 쪽이 쓰였는지는 이 함수가
    받은 ``threads_texts``의 유무로만 갈린다.

    전용 원고에는 원고의 그림을 **전부** 나눠 싣는다: 표지는 첫 스레드, 나머지는 뒤
    스레드에 순서대로 한 장씩(모자라면 없는 스레드도 있고, 남으면 마지막 스레드에
    몰린다 — 스레드당 안전선 MAX_IMAGES_PER_THREAD는 지킨다). 글은 새로 썼으므로
    그림의 '제자리'는 알 수 없지만, 예전처럼 표지 한 장만 붙이면 나머지 그림이 통째로
    버려졌다(2026-08-10 사용자: "스레드에는 첫 번째 이미지만 들어갔어").
    """
    texts = [text.strip() for text in (job.threads_texts or []) if text and text.strip()]
    if texts:
        pieces = [ThreadPiece(text=text) for text in texts]
        images = post_images_of(job.final_post)
        if images:
            stacks: list[list[str]] = [[] for _ in pieces]
            stacks[0].append(images[0])
            # 둘째 그림부터는 둘째 스레드부터 한 장씩. 스레드가 하나뿐이면 전부 첫 스레드다.
            targets = list(range(1, len(pieces))) or [0]
            for order, image in enumerate(images[1:]):
                slot = targets[order] if order < len(targets) else targets[-1]
                if len(stacks[slot]) >= MAX_IMAGES_PER_THREAD:
                    logger.warning(
                        "스레드 이미지 안전선(%d장) 초과 - 남은 그림 %d장은 싣지 않습니다.",
                        MAX_IMAGES_PER_THREAD,
                        len(images) - 1 - order,
                    )
                    break
                stacks[slot].append(image)
            pieces = [
                ThreadPiece(text=piece.text, images=tuple(stack)) if stack else piece
                for piece, stack in zip(pieces, stacks, strict=True)
            ]
        return pieces
    if job.final_post is None:
        return []
    return split_final_post(job.final_post)
