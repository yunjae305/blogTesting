"""네이버 자동발행용 중간 표현(NaverPublishPlan) 생성.

예전 방식(article_segments)은 이미지 태그를 정규식으로 잘라 HTML 조각과 이미지를
번갈아 붙여넣었다. 그 방식은 붙여넣기 사이의 캐럿 위치·서식 상태에 의존해 이미지가
문장 중간에 들어가거나 소제목 굵기가 본문으로 번졌다. 여기서는 본문 전체를 한 번에
붙여넣을 수 있는 "스캐폴드 HTML"을 만들고, 이미지 자리는 우연히 등장할 수 없는
앵커 토큰 문단(<p>__BLOGIT_IMAGE_XXXXXX_001__</p>)으로 치환한다. 에디터는 스캐폴드를
1회 붙여넣은 뒤 앵커만 실제 이미지로 바꾼다.

HTML은 정규식이 아니라 표준 라이브러리 HTMLParser로 해석한다. 파싱하며 동시에:
- 바깥 <article> 래퍼 제거
- FinalPost.title과 같은 <h1> 제거(네이버 제목 칸과 중복 방지), 다른 h1은 h2로 강등
- data URL 이미지를 디코드해 앵커로 치환 — 디코드 실패는 조용히 건너뛰지 않고 오류
- script/style/iframe 등 위험 태그와 class/style/data-*/aria-*/on* 속성 제거
- 인라인 태그(strong 등)가 블록 경계를 넘지 않게 블록 끝에서 강제로 닫음
- 발행 후 DOM 검증에 쓸 기대 텍스트 블록·앵커 앞뒤 텍스트를 수집
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

from app.shared import FinalPost

logger = logging.getLogger(__name__)

# 에디터·검증이 앵커를 찾을 때 쓰는 패턴. 접미사는 post_id 해시라 글마다 다르다.
ANCHOR_TOKEN_PATTERN = re.compile(r"__BLOGIT_IMAGE_[0-9A-F]{6}_\d{3}__")

# 과거 원고에 남아 있을 수 있는 스티커 자리 표식. 스티커 삽입 기능은 제거됐지만 이 독립
# 문단을 그대로 발행하면 내부 마커가 글자로 새므로 스캐폴드를 만들 때 계속 걷어낸다.
STICKER_MARKER_PATTERN = re.compile(
    r"^\s*\[\[\s*STICKER\s*[:：]\s*[^\]]+?\s*\]\]\s*$", re.IGNORECASE
)

# DOM innerText와 비교하므로 zero-width·nbsp까지 공백 하나로 접는다.
_WHITESPACE = re.compile(r"[\s\u200b\ufeff\u00a0]+")


def normalize_text(value: str) -> str:
    """스캐폴드 기대 텍스트와 에디터 innerText를 같은 규칙으로 정규화한다."""
    return _WHITESPACE.sub(" ", value or "").strip()


# --- 제목 정리 ----------------------------------------------------------------
#
# 네이버 제목 칸은 글 목록·검색 결과·이웃 피드에 그대로 실리는 **한 줄**이다. 원고 제목에
# 남은 장식(마크다운 기호, 감싼 따옴표, 앞머리 라벨, 이모지, 반복 구두점)은 본문에서는
# 눈에 덜 띄지만 제목 한 줄에서는 전부 잡음이 된다.
#
# 뜻은 건드리지 않는다 — 지우는 것은 **장식뿐**이고 낱말은 하나도 빼지 않는다. 제목을
# 짧게 만드는 일(줄바꿈을 없애는 일)은 여기서 하지 않는다: 그건 약속을 바꾸는 것이라
# 제목 생성 단계가 할 일이다.

# 네이버 제목 입력 한도. 넘기면 네이버가 조용히 자르는데, 그러면 붙여넣은 제목과 에디터
# 제목이 달라져 발행 전 검증(_check_publish_plan)이 글을 통째로 막는다. 우리가 먼저
# 자르고 로그를 남기는 편이 낫다 — 원고 생성은 60자를 상한으로 두므로 평소엔 닿지 않는다.
NAVER_TITLE_MAX_CHARS = 100

# 마크다운 강조. `**굵게**`·`__굵게__`·`*기울임*` 형태만 벗긴다 — 짝이 맞을 때만이라
# 낱말 안의 별표·밑줄(모델명 등)은 그대로 남는다.
_MD_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1")
# 줄 앞의 마크다운 제목 기호(`## 제목`).
_MD_HEADING = re.compile(r"^\s*#{1,6}\s+")
# 제목 전체를 감싼 따옴표·괄호. 양쪽이 짝일 때만 벗긴다.
_WRAPPING_PAIRS = (
    ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    ("«", "»"), ("『", "』"), ("「", "」"), ("《", "》"), ("〈", "〉"),
)
# 앞머리 라벨(`[리뷰]`, `【정보】`, `<정리>`). 뒤에 실제 내용이 남을 때만 뗀다.
_LEADING_LABEL = re.compile(r"^\s*[\[\(【〔<]{1}[^\]\)】〕>]{1,20}[\]\)】〕>]{1}\s*")
# 이모지·그림문자·변형 선택자. 제목 한 줄에서는 잡음이다.
_PICTOGRAPHS = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\U0000FE00-\U0000FE0F\U00002190-\U000021FF\U00002B00-\U00002BFF]+"
)
# 반복 구두점과 말줄임. `!!!`·`???`·`...`는 제목을 흐린다.
_REPEATED_BANG = re.compile(r"!{2,}")
_REPEATED_QUESTION = re.compile(r"\?{2,}")
_ELLIPSIS = re.compile(r"(\.{2,}|…+)")
# 구두점 앞의 공백과, 여는 괄호 뒤·닫는 괄호 앞의 공백.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%\)\]}])")
_SPACE_AFTER_OPEN = re.compile(r"([\(\[{])\s+")
# 끝에 남은 마침표·쉼표·가운뎃점·구분자. 물음표와 느낌표는 뜻이 있으므로 남긴다.
_TRAILING_NOISE = re.compile(r"[\s.,·|/\-–—~]+$")
_LEADING_NOISE = re.compile(r"^[\s·|/\-–—~]+")


def _strip_wrapping_pair(value: str) -> str:
    """제목 전체를 감싼 따옴표·홑괄호를 벗긴다. 짝이 맞을 때만, 반복해서."""
    text = value
    for _ in range(3):  # '"[제목]"'처럼 두 겹까지. 무한 루프를 만들지 않는다.
        stripped = text.strip()
        for opening, closing in _WRAPPING_PAIRS:
            if (
                len(stripped) > 2
                and stripped.startswith(opening)
                and stripped.endswith(closing)
                # 안쪽에 같은 기호가 또 있으면 감싼 것이 아니라 인용이 섞인 제목이다.
                and closing not in stripped[1:-1]
            ):
                text = stripped[1:-1]
                break
        else:
            return text.strip()
    return text.strip()


def naver_title(raw: str) -> str:
    """네이버 제목 칸에 넣을 한 줄. 뜻은 그대로 두고 장식만 걷어낸다.

    붙여넣기와 발행 전 검증이 **같은 문자열**을 쓴다(둘 다 plan.title을 본다). 그래서
    여기서 무엇을 바꾸든 '에디터 제목이 계획과 다르다'로 이어지지 않는다.
    """
    text = _MD_HEADING.sub("", raw or "")
    # 강조 기호는 중첩될 수 있다(`**"제목"**`). 더 줄지 않을 때까지 반복한다.
    for _ in range(3):
        unwrapped = _MD_EMPHASIS.sub(r"\2", text)
        if unwrapped == text:
            break
        text = unwrapped
    text = text.replace("`", "")
    text = _PICTOGRAPHS.sub(" ", text)
    text = _strip_wrapping_pair(text)

    labelled = _LEADING_LABEL.sub("", text)
    # 라벨만 있는 제목(`[리뷰]`)은 그대로 둔다 — 빈 제목을 만들지 않는다.
    if labelled.strip():
        text = labelled

    text = _REPEATED_BANG.sub("!", text)
    text = _REPEATED_QUESTION.sub("?", text)
    text = _ELLIPSIS.sub(" ", text)
    text = normalize_text(text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _LEADING_NOISE.sub("", text)
    text = _TRAILING_NOISE.sub("", text)
    text = normalize_text(text)

    if len(text) > NAVER_TITLE_MAX_CHARS:
        # 낱말 중간에서 자르지 않는다. 네이버가 자르는 것보다 우리가 자르는 편이 낫다 —
        # 어디서 잘렸는지 로그에 남고, 붙여넣기와 검증이 같은 문자열을 쓴다.
        cut = text[:NAVER_TITLE_MAX_CHARS]
        space = cut.rfind(" ")
        if space >= NAVER_TITLE_MAX_CHARS // 2:
            cut = cut[:space]
        logger.warning(
            "네이버 제목이 %d자를 넘어 잘랐습니다: %r → %r",
            NAVER_TITLE_MAX_CHARS,
            text,
            cut,
        )
        text = _TRAILING_NOISE.sub("", cut)
    return text


class NaverPlanError(ValueError):
    """발행 계획을 만들 수 없는 원고. 발행은 시작조차 하지 않는다(fail-closed)."""


@dataclass(frozen=True)
class NaverImageAnchor:
    index: int
    token: str
    image_bytes: bytes
    alt_text: str
    caption: str | None
    expected_previous_text: str | None
    expected_next_text: str | None


@dataclass(frozen=True)
class NaverPublishPlan:
    title: str
    scaffold_html: str
    scaffold_plain_text: str
    image_anchors: tuple[NaverImageAnchor, ...]
    # 발행 후 DOM에 이 순서 그대로 나타나야 하는 텍스트 조각들 (문단·소제목·목록 항목 단위)
    expected_text_blocks: tuple[str, ...]
    # 전체가 굵게 보이면 안 되는 일반 본문 문단 (소제목 굵기 번짐 검증용)
    plain_paragraph_texts: tuple[str, ...]


# --- 파서 --------------------------------------------------------------------

_INLINE_TAGS = {"strong", "em", "mark", "s", "code", "a", "br"}
# 최상위 블록. h1은 제목 중복 제거·강등 때문에 따로 다룬다.
_TOP_BLOCK_TAGS = {"p", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "table"}
# 블록 내부 구조 태그. 닫힐 때 기대 텍스트 한 항목이 끝난다(li·th·td·중첩 p).
_ENTRY_TAGS = {"li", "th", "td", "p"}
_STRUCT_TAGS = {"li", "thead", "tbody", "tr", "th", "td", "p"}
# 내용까지 통째로 버리는 태그 — 태그만 벗기면 코드가 글자로 남는다.
_DROP_CONTENT_TAGS = {
    "script", "style", "noscript", "iframe", "object", "embed", "template", "svg", "canvas",
}
_KEEP_ATTRS = {"a": {"href", "title"}, "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"}}
_SAFE_HREF_PREFIXES = ("http://", "https://", "mailto:")
# 붙여넣기에서 의미가 같은 구식 태그는 표준형으로 바꾼다.
_TAG_ALIASES = {"b": "strong", "i": "em", "strike": "s", "del": "s"}

# 문단 사이를 벌리는 빈 문단. `<p></p>`·`<p>&nbsp;</p>`는 우리 파서가 내용 없는 블록으로
# 보고 버리므로(_close_block의 마지막 조건) <br />로 채운다 — 실측으로 이 형태만 남는다.
_BLANK_PARAGRAPH = "<p><br /></p>"


def _decode_data_image(src: str) -> bytes:
    if not src.lower().startswith("data:image/"):
        raise NaverPlanError(
            f"본문 이미지가 data URL이 아니라 붙여넣을 바이트를 만들 수 없습니다: {src[:80]}"
        )
    try:
        _, encoded = src.split(",", 1)
        raw = base64.b64decode(encoded)
    except Exception as error:
        raise NaverPlanError(f"본문 이미지 data URL을 디코드하지 못했습니다: {error}") from error
    if not raw:
        raise NaverPlanError("본문 이미지 data URL이 비어 있습니다.")
    return raw


class _Block:
    """스캐폴드를 이룰 블록 하나. text 블록이거나 이미지 앵커다."""

    __slots__ = (
        "kind", "tag", "body", "entries", "text_len", "strong_len", "em_len",
        "token", "image_bytes", "alt_text",
    )

    def __init__(self, kind: str, tag: str = ""):
        self.kind = kind          # "text" | "anchor"
        self.tag = tag
        self.body: list[str] = []
        self.entries: list[str] = []
        self.text_len = 0
        self.strong_len = 0
        self.em_len = 0
        self.token = ""
        self.image_bytes = b""
        self.alt_text = ""

    @property
    def all_strong(self) -> bool:
        return self.text_len > 0 and self.strong_len == self.text_len

    @property
    def all_em(self) -> bool:
        return self.text_len > 0 and self.em_len == self.text_len

    def html(self) -> str:
        if self.kind == "anchor":
            return f"<p>{self.token}</p>"
        if self.tag == "hr":
            return "<hr />"
        return f"<{self.tag}>{''.join(self.body)}</{self.tag}>"


class _ScaffoldParser(HTMLParser):
    def __init__(self, title: str, next_token) -> None:
        super().__init__(convert_charrefs=True)
        # 본문의 h1을 제목과 대조해 빼는 데 쓴다. 원고의 h1은 **정리 전** 제목이므로
        # 원문과 정리본을 둘 다 인정한다 — 하나만 보면 장식이 붙은 h1이 본문에 남아
        # 제목이 두 번 나오는 글이 된다.
        #: 제목으로 인정하는 문자열들. 조립 단계(build_naver_publish_plan)도 본다 —
        #: 본문 첫 줄이 제목을 되풀이하는지 거기서 한 번 더 가려낸다.
        self.titles = {
            normalized
            for normalized in (normalize_text(title), naver_title(title))
            if normalized
        }
        self._next_token = next_token
        self.blocks: list[_Block] = []
        self._block: _Block | None = None
        self._open: list[str] = []      # 블록 안에서 열린 태그 (구조+인라인)
        self._entry: list[str] = []     # 현재 기대 텍스트 항목
        self._muted = 0                 # _DROP_CONTENT_TAGS 내부
        self._strong_depth = 0
        self._em_depth = 0
        self._in_figcaption = 0
        self._figcaption: list[str] = []
        self._title_removed = False

    # --- 블록 수명 -----------------------------------------------------------

    def _open_block(self, tag: str) -> None:
        self._close_block()
        self._block = _Block("text", tag)

    def _flush_entry(self) -> None:
        if self._block is None:
            self._entry.clear()
            return
        text = normalize_text("".join(self._entry))
        self._entry.clear()
        if text:
            self._block.entries.append(text)

    def _close_block(self) -> None:
        block = self._block
        if block is None:
            self._entry.clear()
            self._open.clear()
            return
        # 닫히지 않은 태그를 모두 닫는다 — strong이 블록 경계를 넘지 않게 하는 지점.
        while self._open:
            tag = self._open.pop()
            block.body.append(f"</{tag}>")
            if tag in _ENTRY_TAGS:
                self._flush_entry()
        self._flush_entry()
        self._block = None
        self._strong_depth = 0
        self._em_depth = 0
        if block.tag == "h1":
            # 제목과 같은 h1은 본문에서 뺀다(제목 칸과 중복). 다른 h1은 h2로 강등.
            text = " ".join(block.entries)
            if not self._title_removed and normalize_text(text) in self.titles:
                self._title_removed = True
                return
            block.tag = "h2"
        if block.entries or block.tag == "hr" or "".join(block.body).strip():
            self.blocks.append(block)

    def _append_anchor(self, src: str, alt: str) -> None:
        image_bytes = _decode_data_image(src)
        block = _Block("anchor")
        block.token = self._next_token()
        block.image_bytes = image_bytes
        block.alt_text = alt
        self.blocks.append(block)

    # --- HTMLParser 훅 -------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _TAG_ALIASES.get(tag, tag)
        if tag in _DROP_CONTENT_TAGS:
            self._muted += 1
            return
        if self._muted:
            return
        values = {key: (value or "") for key, value in attrs}

        if tag == "img":
            # 이미지는 스캐폴드에 남기지 않는다 — 문단 중간이면 문단을 앵커 앞에서 끊는다.
            self._close_block()
            self._append_anchor(values.get("src", ""), values.get("alt", ""))
            return
        if tag == "figure":
            self._close_block()
            return
        if tag == "figcaption":
            self._in_figcaption += 1
            return
        if self._in_figcaption:
            return
        if tag == "hr":
            self._close_block()
            block = _Block("text", "hr")
            self.blocks.append(block)
            return
        if tag == "h1":
            self._open_block("h1")
            return
        if tag in _TOP_BLOCK_TAGS:
            # blockquote 안의 p처럼 유효한 중첩만 내부 태그로 남긴다.
            if self._block is not None and tag == "p" and self._block.tag == "blockquote":
                self._block.body.append("<p>")
                self._open.append("p")
                return
            self._open_block(tag)
            return
        if self._block is None:
            if tag in _INLINE_TAGS or tag in _STRUCT_TAGS:
                # 블록 밖에서 시작한 내용은 암묵적 문단으로 받는다.
                self._open_block("p")
            else:
                return

        if tag == "br":
            self._block.body.append("<br />")
            self._entry.append(" ")
            return
        if tag in _STRUCT_TAGS:
            kept = {
                key: value
                for key, value in values.items()
                if key in _KEEP_ATTRS.get(tag, set())
            }
            rendered = "".join(
                f' {key}="{escape(value, quote=True)}"' for key, value in kept.items()
            )
            self._block.body.append(f"<{tag}{rendered}>")
            self._open.append(tag)
            return
        if tag in _INLINE_TAGS:
            if tag == "a":
                href = values.get("href", "")
                if not href.strip().lower().startswith(_SAFE_HREF_PREFIXES):
                    return  # 글자는 남기고 링크만 버린다
            kept = {
                key: value
                for key, value in values.items()
                if key in _KEEP_ATTRS.get(tag, set())
            }
            rendered = "".join(
                f' {key}="{escape(value, quote=True)}"' for key, value in kept.items()
            )
            self._block.body.append(f"<{tag}{rendered}>")
            self._open.append(tag)
            if tag == "strong":
                self._strong_depth += 1
            elif tag == "em":
                self._em_depth += 1
            return
        # 허용 목록 밖 태그(div·span·section…)는 태그만 벗기고 내용은 흘려보낸다.

    def handle_endtag(self, tag: str) -> None:
        tag = _TAG_ALIASES.get(tag, tag)
        if tag in _DROP_CONTENT_TAGS:
            self._muted = max(0, self._muted - 1)
            return
        if self._muted:
            return
        if tag == "figcaption":
            self._in_figcaption = max(0, self._in_figcaption - 1)
            if not self._in_figcaption:
                caption = normalize_text("".join(self._figcaption))
                self._figcaption.clear()
                if caption:
                    block = _Block("text", "p")
                    block.body.append(f"<em>{escape(caption)}</em>")
                    block.entries.append(caption)
                    length = len(caption)
                    block.text_len = block.em_len = length
                    self.blocks.append(block)
            return
        if self._in_figcaption:
            return
        if tag in ("figure", "article"):
            self._close_block()
            return
        if self._block is None:
            return
        if tag == "h1" or (tag in _TOP_BLOCK_TAGS and tag == self._block.tag and tag not in self._open):
            self._close_block()
            return
        if tag not in self._open:
            return
        # 중간에 안 닫힌 태그가 있으면 함께 닫는다(출력은 항상 온전한 HTML).
        while self._open:
            current = self._open.pop()
            self._block.body.append(f"</{current}>")
            if current in _ENTRY_TAGS:
                self._flush_entry()
            if current == "strong":
                self._strong_depth = max(0, self._strong_depth - 1)
            elif current == "em":
                self._em_depth = max(0, self._em_depth - 1)
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._muted:
            return
        if self._in_figcaption:
            self._figcaption.append(data)
            return
        if self._block is None:
            if not data.strip():
                return
            self._open_block("p")
        self._block.body.append(escape(data))
        self._entry.append(data)
        visible = len(normalize_text(data))
        self._block.text_len += visible
        if self._strong_depth:
            self._block.strong_len += visible
        if self._em_depth:
            self._block.em_len += visible

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._close_block()


# --- 계획 조립 ----------------------------------------------------------------


def _join_with_blank_lines(blocks: list[_Block]) -> str:
    """블록을 이어 붙이되, 글 블록끼리 맞닿는 자리에만 빈 문단을 하나 끼운다.

    그냥 이어 붙이면 `</p><p>`·`</p><h2>`가 되어 네이버 에디터에서 문단이 빈 줄 없이
    딱 붙는다(실측: 발행된 글의 본문 문단 사이 빈 문단 0개).

    **이미지 앵커와 맞닿는 자리는 건드리지 않는다.** 이유가 둘 있다. 앵커 다음 블록이
    캡션인지 보는 판정은 이 함수보다 앞에서 끝나지만(캡션은 이미 blocks에서 빠졌다),
    앵커 주변에 문단을 늘리면 앵커를 클릭해 줄을 선택하는 삽입 경로가 겪는 문서 높이가
    달라진다 — 2026-08-03에 고친 '이미지가 앵커 아닌 곳에 삽입' 부류를 다시 부를 여지를
    만들지 않는다. 이미지 컴포넌트는 네이버가 자체 여백을 주므로 시각적으로도 필요없다.
    """
    parts: list[str] = []
    for index, block in enumerate(blocks):
        if index and block.kind == "text" and blocks[index - 1].kind == "text":
            parts.append(_BLANK_PARAGRAPH)
        parts.append(block.html())
    return "".join(parts)


def build_naver_publish_plan(post: FinalPost, post_id: str) -> NaverPublishPlan:
    """FinalPost를 네이버 붙여넣기 계약(스캐폴드 1회 + 앵커 교체)으로 바꾼다."""
    # 네이버 제목 칸에 실제로 들어갈 한 줄. 장식만 걷어낸 것이라 뜻은 원고 제목과 같다.
    title = naver_title(post.title)
    if not title:
        raise NaverPlanError("발행할 제목이 비어 있습니다.")

    suffix = hashlib.sha1(post_id.encode("utf-8")).hexdigest()[:6].upper()
    counter = {"n": 0}

    def next_token() -> str:
        counter["n"] += 1
        return f"__BLOGIT_IMAGE_{suffix}_{counter['n']:03d}__"

    parser = _ScaffoldParser(post.title, next_token)
    parser.feed(post.html_content or "")
    parser.close()
    blocks = parser.blocks

    # 과거 원고의 스티커 마커가 발행물에 글자로 남지 않게 걷어낸다.
    kept_blocks: list[_Block] = []
    for block in blocks:
        marker = (
            STICKER_MARKER_PATTERN.match(" ".join(block.entries).strip())
            if block.kind == "text" and block.entries
            else None
        )
        if marker is None:
            kept_blocks.append(block)
    blocks = kept_blocks
    if not blocks:
        raise NaverPlanError("발행할 본문 내용이 없습니다.")

    # 캡션: 앵커 바로 다음 블록이 전체 <em> 문단이면 그 사진의 설명(출처 표기)이다.
    # 스캐폴드에서 **빼고** 앵커에 실어 보낸다 — 에디터가 네이버 '사진 설명' 칸에 넣는다.
    # 본문 문단으로도 남겨 두면 사진 아래에 같은 출처가 두 번 찍힌다.
    captions: dict[int, str] = {}
    caption_positions: set[int] = set()
    for position, block in enumerate(blocks):
        if block.kind != "anchor" or position + 1 >= len(blocks):
            continue
        follower = blocks[position + 1]
        if follower.kind == "text" and follower.tag == "p" and follower.all_em:
            caption = " ".join(follower.entries).strip()
            if caption:
                captions[id(block)] = caption
                caption_positions.add(position + 1)
    if caption_positions:
        blocks = [b for i, b in enumerate(blocks) if i not in caption_positions]

    # 본문 **첫 글줄**이 제목을 그대로 되풀이하면 뺀다.
    #
    # 파서도 제목과 같은 `<h1>`을 하나 빼지만(그것 하나뿐이다), 원고에는 제목이 두 번
    # 들어오는 일이 있다. 실제로 이렇게 나왔다(2026-08-10 예약 포스팅):
    #
    #     <h1>제목</h1> → <figure><img></figure> → <p><em>출처: …</em></p> → <h1>제목</h1>
    #
    # 앞의 h1은 파서가 빼고 뒤의 h1은 h2로 강등돼 남는데, 그 사이의 출처 문단은 바로
    # 위에서 사진 설명으로 빠져나간다. 그래서 **여기까지 와야** 무엇이 본문 첫 줄인지
    # 알 수 있다 — 파싱 시점의 '맨 앞'으로 판단하면 이 경우를 놓친다.
    #
    # 그대로 두면 네이버 제목 칸과 본문 첫 줄이 같은 문장이 되고, 발행 전 검증이
    # '제목 본문 중복'으로 글을 통째로 막는다(발행 버튼도 눌러 보지 못한다).
    #
    # **첫 글줄 하나만** 본다. 글 한가운데에서 제목과 같은 문장이 나오는 것은 본문의
    # 일부일 수 있고, 검증이 보는 것도 첫 블록 하나뿐이다.
    for position, block in enumerate(blocks):
        if block.kind != "text" or not block.entries:
            continue  # 이미지 앵커·구분선은 글줄이 아니다 — 지나간다.
        if normalize_text(" ".join(block.entries)) in parser.titles:
            blocks = [b for i, b in enumerate(blocks) if i != position]
        break

    if not blocks:
        raise NaverPlanError("발행할 본문 내용이 없습니다.")

    scaffold_html = _join_with_blank_lines(blocks)
    if "data:image" in scaffold_html:
        raise NaverPlanError("스캐폴드에 data URL 이미지가 남았습니다 — 앵커 치환이 누락됐습니다.")

    # 기대 텍스트를 순서대로 편다. 앵커 위치는 앞뒤 항목 인덱스로 기억한다.
    expected: list[str] = []
    plain_paragraphs: list[str] = []
    anchor_neighbors: list[int] = []  # 앵커 직전까지의 기대 항목 수
    anchors_raw: list[_Block] = []
    for block in blocks:
        if block.kind == "anchor":
            anchors_raw.append(block)
            anchor_neighbors.append(len(expected))
            continue
        expected.extend(block.entries)
        if block.tag == "p" and not block.all_strong:
            plain_paragraphs.extend(block.entries)

    anchors: list[NaverImageAnchor] = []
    for index, (block, before_count) in enumerate(zip(anchors_raw, anchor_neighbors)):
        previous_text = expected[before_count - 1] if before_count > 0 else None
        next_text = expected[before_count] if before_count < len(expected) else None
        anchors.append(
            NaverImageAnchor(
                index=index,
                token=block.token,
                image_bytes=block.image_bytes,
                alt_text=block.alt_text,
                caption=captions.get(id(block)),
                expected_previous_text=previous_text,
                expected_next_text=next_text,
            )
        )

    plain_parts: list[str] = []
    for block in blocks:
        if block.kind == "anchor":
            plain_parts.append(block.token)
        else:
            plain_parts.extend(block.entries)
    scaffold_plain_text = "\n\n".join(plain_parts)

    return NaverPublishPlan(
        title=title,
        scaffold_html=scaffold_html,
        scaffold_plain_text=scaffold_plain_text,
        image_anchors=tuple(anchors),
        expected_text_blocks=tuple(expected),
        plain_paragraph_texts=tuple(plain_paragraphs),
    )
