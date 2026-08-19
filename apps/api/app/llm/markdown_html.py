"""원고 마크다운 → 발행용 HTML 변환.

원고 모델은 이제 본문을 마크다운 **한 벌**만 쓴다. 예전에는 스키마가 body·
htmlContent·markdownContent 세 벌을 요구해 모델이 같은 글을 세 번 출력했고,
출력 토큰이 곧 생성 시간이라 원고 단계가 세 배로 느렸다(같은 글을 손으로 세 번
베껴 쓰는 것과 같다). 서식 변환은 판단이 아니라 기계적 작업이므로 코드가 맡는다.

지원 문법은 원고 프롬프트가 지시하는 것과 정확히 같다: `## `/`### ` 소제목,
빈 줄 구분 문단, `**굵게**`, `==형광펜==`, `-`/`1.` 목록, 마크다운 표,
`[[VISUAL: id]]` 마커. 그 밖의 문법은 텍스트로 남는다 — 지시하지 않은 문법을
조용히 지원하면 프롬프트와 변환기가 서로 다른 규격을 갖게 된다.

출력 형태는 기존 모델 산출물과 같은 `<article><h1>제목</h1>…</article>` 골격을
유지한다. 네이버 발행(article 래퍼 제거), 프론트 편집기(article 언래핑), 카드
배치(</h2> 순번 탐색)가 모두 이 골격을 전제한다.
"""

from __future__ import annotations

import re

from app.shared.format import escape_html

# 소제목 순번 배치(insert_after_heading_*)는 markdown의 `^## `와 html의 `</h2>`를
# 같은 순번으로 짝짓는다. 그래서 변환은 반드시 1:1이어야 한다 — `## `만 <h2>가 되고,
# 다른 무엇도 <h2>가 되지 않는다.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}[-\s:|]*$")
# 시각자료·(구형) 이미지 마커만으로 이루어진 블록은 <p>로 감싸지 않는다 — 치환될 때
# <figure>가 들어오는데, <p> 안의 <figure>는 유효하지 않은 HTML이고 편집기·발행
# 경로가 구조를 임의로 고쳐 버린다.
_MARKER_ONLY = re.compile(r"^\[\[(?:VISUAL|IMAGE):[^\]]+\]\]$", re.IGNORECASE)

_MARK_SPAN = re.compile(r"==(.+?)==", re.DOTALL)

# 마크다운 이미지 한 개(`![alt](src)`), 캡션 별표는 제외. 형광펜 치환이 이 구간을
# 건드리면 안 된다 — base64 data URL의 패딩이 `==`라, 이미지가 두 장 이상이면
# `==(.+?)==`가 첫 이미지의 패딩을 여는 기호로, 다음 이미지의 패딩을 닫는 기호로
# 오인해 둘 다 `**`로 바꿔 놓는다(2026-08-07 실측: 저장된 글의 썸네일 src가 `…2Q**`로
# 끝나 미리보기가 깨지고, 이미지 외부화의 문자열 매칭도 빗나가 원본 base64가
# markdown에 그대로 남았다).
_IMAGE_SEGMENT = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")


def _inline(text: str) -> str:
    escaped = escape_html(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.DOTALL)
    escaped = _MARK_SPAN.sub(r"<mark>\1</mark>", escaped)
    return escaped


def _table_html(lines: list[str]) -> str | None:
    rows: list[list[str]] = []
    for line in lines:
        if _TABLE_SEPARATOR.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells:
            rows.append(cells)
    if len(rows) < 2:
        return None
    header, body = rows[0], rows[1:]
    head_html = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in body
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


def _list_html(lines: list[str], ordered: bool) -> str:
    pattern = _ORDERED if ordered else _BULLET
    items = "".join(
        f"<li>{_inline(match.group(1))}</li>"
        for line in lines
        if (match := pattern.match(line))
    )
    tag = "ol" if ordered else "ul"
    return f"<{tag}>{items}</{tag}>"


def _looks_like_table(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and all("|" in line for line in lines)
        and any(_TABLE_SEPARATOR.match(line) for line in lines[:3])
    )


def _is_table_row(line: str) -> bool:
    """`| 셀 | 셀 |` 모양의 한 줄인가. 앞머리 파이프까지 요구해 본문 문장을 오인하지 않는다."""
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _merge_table_blocks(blocks: list[str]) -> list[str]:
    """행 사이에 빈 줄을 넣어 온 표를 한 블록으로 다시 모은다.

    문단 분리가 빈 줄이라, 모델이 표의 행마다 빈 줄을 넣으면 행 하나하나가 별개 블록이
    되어 `| 구분 | 스탠딩석 |`이 글자 그대로 문단으로 찍힌다. 실제로 그렇게 나왔다.
    모아 놓고 봤을 때 구분선(`|---|`)까지 갖춘 진짜 표일 때만 합치고, 아니면 원래 블록을
    그대로 둔다 — 파이프가 든 문장을 표로 착각해 묶지 않기 위해서다.
    """
    merged: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        lines = [line for block in run for line in block.split("\n") if line.strip()]
        if len(run) > 1 and _looks_like_table(lines):
            merged.append("\n".join(lines))
        else:
            merged.extend(run)
        run.clear()

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if lines and all(_is_table_row(line) for line in lines):
            run.append(block)
            continue
        flush()
        merged.append(block)
    flush()
    return merged


def _block_html(block: str) -> str:
    lines = [line for line in block.split("\n") if line.strip()]
    if not lines:
        return ""

    if len(lines) == 1:
        stripped = lines[0].strip()
        if _MARKER_ONLY.match(stripped):
            return stripped
        heading = _HEADING.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 6)
            return f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>"

    if _looks_like_table(lines):
        table = _table_html(lines)
        if table:
            return table

    if all(_BULLET.match(line) for line in lines):
        return _list_html(lines, ordered=False)
    if all(_ORDERED.match(line) for line in lines):
        return _list_html(lines, ordered=True)

    # 소제목이 문단과 한 블록에 붙어 온 경우(모델이 빈 줄을 빠뜨림)도 소제목을 살린다.
    parts: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            parts.append("<p>" + "<br />".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    for line in lines:
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)), 6)
            parts.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
        elif _MARKER_ONLY.match(stripped):
            flush()
            parts.append(stripped)
        else:
            paragraph.append(stripped)
    flush()
    return "".join(parts)


def markdown_to_html(title: str, markdown: str) -> str:
    """마크다운 원고를 `<article><h1>…</h1>…</article>`로 변환한다.

    첫 블록이 h1이면 건너뛴다 — 제목의 근원은 finalPost.title 하나이고, 그대로 두면
    제목이 두 번 찍힌다(프론트 blocksFromMarkdown과 같은 규칙).
    """
    blocks = _merge_table_blocks(re.split(r"\n\s*\n", markdown.strip()))
    rendered: list[str] = []
    for index, block in enumerate(blocks):
        stripped = block.strip()
        if not stripped:
            continue
        if index == 0:
            heading = _HEADING.match(stripped)
            if heading and len(heading.group(1)) == 1 and "\n" not in stripped:
                continue
        html = _block_html(stripped)
        if html:
            rendered.append(html)
    return f"<article><h1>{escape_html(title)}</h1>{''.join(rendered)}</article>"


def markdown_for_storage(title: str, markdown: str) -> str:
    """저장·프론트 편집기로 나가는 마크다운.

    - `==형광펜==`은 마크다운 표준에 없어 편집기에 `==`가 글자 그대로 새므로
      `**굵게**`로 바꾼다(프론트 markdownFromHtml의 MARK→** 규칙과 동일).
      형광펜 의미는 htmlContent가 <mark>로 유지한다.
    - 첫 줄을 `# 제목`으로 정규화한다 — 카드 배치·미리보기가 이 골격을 전제한다.
    - 형광펜 치환 전에 이미지 구간을 빼 둔다(_IMAGE_SEGMENT 주석 참고 — base64 패딩
      `==`가 형광펜 기호로 오인되어 이미지가 깨졌다).
    """
    stashed: list[str] = []

    def stash(match: re.Match) -> str:
        stashed.append(match.group(0))
        return f"\x00IMG{len(stashed) - 1}\x00"

    text = _IMAGE_SEGMENT.sub(stash, markdown.strip())
    text = _MARK_SPAN.sub(r"**\1**", text)
    for index, segment in enumerate(stashed):
        text = text.replace(f"\x00IMG{index}\x00", segment, 1)
    # 행 사이에 빈 줄이 낀 표는 저장 마크다운에서도 표가 아니다(편집기·복사·본문 검사가
    # 같은 문자열을 본다). HTML과 같은 규칙으로 한 블록으로 되돌린다.
    text = "\n\n".join(_merge_table_blocks(re.split(r"\n\s*\n", text)))
    blocks = re.split(r"\n\s*\n", text, maxsplit=1)
    first = blocks[0].strip() if blocks else ""
    heading = _HEADING.match(first)
    if heading and len(heading.group(1)) == 1 and "\n" not in first:
        rest = blocks[1] if len(blocks) > 1 else ""
        return f"# {title}\n\n{rest.strip()}".strip()
    return f"# {title}\n\n{text}".strip()
