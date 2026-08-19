"""게시 브라우저 로그에 OAuth code·계정 경로·query를 남기지 않는 URL 표현."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit


def safe_url_for_log(value: object) -> str:
    """Return origin plus an opaque path fingerprint, never query/fragment/path text."""
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "(redacted)"
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path_digest = hashlib.sha256((parsed.path or "/").encode("utf-8")).hexdigest()[:10]
        return f"{parsed.scheme}://{host}/… [path:{path_digest}]"
    except (TypeError, ValueError):
        return "(redacted)"
