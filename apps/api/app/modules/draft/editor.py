"""에디터가 돌려보낸 것을 글로 바꾼다.

이제 클라이언트는 리치 텍스트 에디터라, 수정 결과가 텍스트 줄이 아니라 HTML로 도착한다.
이 HTML은 사용자 입력이다: 네이버에 복사되어 발행되므로 도착한 그대로 저장할 수 없다.
파싱한 뒤 허용 목록에서 다시 세운다 — 목록에 없는 것은 이스케이프하지 않고 버린다.
발행된 글에 <script>가 글자 그대로 살아남는 것 역시, 방식만 다를 뿐 똑같이 잘못이기 때문이다.

나머지 필드도 여기서 유도한다. body와 markdownContent는 htmlContent와 같은 말을 해야 하고,
htmlContent에서 유도하는 것만이 둘이 어긋나지 않게 하는 유일한 방법이다.
"""

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

# 글을 이룰 수 있는 것들. Tiptap이 만들도록 설정된 것 전부, 그리고 그 밖은 없다.
ALLOWED_TAGS = {
    "p",
    "h2",
    "h3",
    "strong",
    # 형광펜 강조. Tiptap Highlight 확장과 짝이다 — 여기 없으면 저장할 때 강조가 사라진다.
    "mark",
    "em",
    "s",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "br",
    "hr",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}

VOID_TAGS = {"br", "hr", "img"}

# 태그별로 유지하는 속성. href와 src는 아래에서 한 번 더 검사한다 — 허용된 속성이
# `javascript:`를 담고 있으면 그게 바로 구멍이다.
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}

SAFE_URL_PREFIXES = ("http://", "https://", "mailto:", "data:image/")

BLOCK_TAGS = {"p", "h2", "h3", "blockquote", "li", "tr", "pre"}

# *내용*이 산문이 아니라 코드인 태그. 태그만 버리면 `alert(1)`이 글에 텍스트로 남는다.
DROP_CONTENT_TAGS = {"script", "style", "noscript", "iframe", "object", "embed", "template"}


def _safe_url(value: str) -> bool:
    return value.strip().lower().startswith(SAFE_URL_PREFIXES)


@dataclass
class EditedPost:
    html: str
    markdown: str
    text: str
    image_srcs: list[str] = field(default_factory=list)


class _Rebuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html: list[str] = []
        self.markdown: list[str] = []
        self.text: list[str] = []
        self.image_srcs: list[str] = []

        self._open: list[str] = []
        self._line: list[str] = []
        self._in_list = 0
        self._ordered = False
        self._muted = 0

    # --- 헬퍼 ------------------------------------------------------------

    def _flush_line(self, prefix: str = "") -> None:
        line = "".join(self._line).strip()
        self._line.clear()
        if not line:
            return
        self.markdown.append(f"{prefix}{line}")
        self.text.append(line)

    # --- 파서 ------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_CONTENT_TAGS:
            self._muted += 1
            return
        if self._muted or tag not in ALLOWED_TAGS:
            return

        values = {key: (value or "") for key, value in attrs}

        if tag == "img":
            src = values.get("src", "")
            if not _safe_url(src):
                return
            alt = values.get("alt", "")
            self.image_srcs.append(src)
            self.html.append(f'<figure><img src="{escape(src, quote=True)}" alt="{escape(alt, quote=True)}" /></figure>')
            self.markdown.append(f"![{alt}]({src})")
            return

        if tag == "a" and not _safe_url(values.get("href", "")):
            # 글자는 남기고 링크만 버린다.
            return

        kept = {
            key: value
            for key, value in values.items()
            if key in ALLOWED_ATTRS.get(tag, set())
        }
        rendered = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in kept.items())
        self.html.append(f"<{tag}{rendered}{' /' if tag in VOID_TAGS else ''}>")

        if tag in VOID_TAGS:
            return

        self._open.append(tag)
        if tag in ("ul", "ol"):
            self._in_list += 1
            self._ordered = tag == "ol"

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_CONTENT_TAGS:
            self._muted = max(0, self._muted - 1)
            return
        if self._muted or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag not in self._open:
            return

        # 에디터가 닫지 않고 둔 태그를 모두 닫는다. 입력이 온전치 않아도 출력은 온전하도록.
        while self._open:
            current = self._open.pop()
            self.html.append(f"</{current}>")

            if current in BLOCK_TAGS:
                if current == "li":
                    self._flush_line("1. " if self._ordered else "- ")
                elif current == "h2":
                    self._flush_line("## ")
                elif current == "h3":
                    self._flush_line("### ")
                elif current == "blockquote":
                    self._flush_line("> ")
                else:
                    self._flush_line()

            if current in ("ul", "ol"):
                self._in_list = max(0, self._in_list - 1)

            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._muted or not data.strip():
            return
        self.html.append(escape(data))
        self._line.append(data)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        while self._open:
            self.html.append(f"</{self._open.pop()}>")
        self._flush_line()


def parse_edited_html(raw: str) -> EditedPost:
    parser = _Rebuilder()
    parser.feed(raw)
    parser.close()

    return EditedPost(
        html=f"<article>{''.join(parser.html)}</article>",
        markdown="\n\n".join(parser.markdown),
        text="\n\n".join(parser.text),
        image_srcs=parser.image_srcs,
    )
