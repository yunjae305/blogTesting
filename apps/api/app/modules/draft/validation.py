"""원고 요청 본문 검증."""

from dataclasses import dataclass
from typing import Any

from app.errors import BlogTaskError
from app.shared import DraftFormat

MAX_STYLE_CHARS = 500
MAX_TITLE_CHARS = 200
MAX_EDIT_HTML_CHARS = 500_000


@dataclass
class GenerateDraftRequest:
    format: DraftFormat
    style: str | None = None


def validate_generate_draft_request(body: Any) -> GenerateDraftRequest:
    if body is None:
        return GenerateDraftRequest(format=DraftFormat.HTML)
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    style = body.get("style")
    fmt = body.get("format")

    if style is not None and (not isinstance(style, str) or not style.strip()):
        raise BlogTaskError("VALIDATION_FAILED", "style must be a non-empty string when provided")
    if isinstance(style, str) and len(style.strip()) > MAX_STYLE_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"style must be at most {MAX_STYLE_CHARS} characters"
        )
    if fmt is not None and fmt not in (DraftFormat.HTML.value, DraftFormat.MARKDOWN.value):
        raise BlogTaskError("VALIDATION_FAILED", "format must be html or markdown")

    return GenerateDraftRequest(
        format=DraftFormat(fmt) if fmt is not None else DraftFormat.HTML,
        style=style.strip() if isinstance(style, str) else None,
    )


def validate_update_draft_request(body: Any) -> tuple[str, str]:
    """사용자가 에디터에서 다시 쓴 원고의 (title, html)을 반환한다."""
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    title = body.get("title")
    html = body.get("html")

    if not isinstance(title, str) or not title.strip():
        raise BlogTaskError("VALIDATION_FAILED", "title is required")
    if not isinstance(html, str) or not html.strip():
        raise BlogTaskError("VALIDATION_FAILED", "html is required")
    if len(title.strip()) > MAX_TITLE_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"title must be at most {MAX_TITLE_CHARS} characters"
        )
    if len(html) > MAX_EDIT_HTML_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"html must be at most {MAX_EDIT_HTML_CHARS} characters"
        )

    return title.strip(), html
