"""네이버 에디터가 잠깐 가져갈 수 있는 만료형 게시 이미지 URL."""

from __future__ import annotations

import time
from urllib.parse import urlencode

from app.modules.auth.token import sign_internal, verify_internal

POST_IMAGE_URL_TTL_SECONDS = 10 * 60
_PURPOSE = "post-image-v1"


def _payload(post_id: str, index: int, expires_at: int) -> str:
    return f"{post_id}\0{index}\0{expires_at}"


def signed_post_image_url(
    api_origin: str,
    post_id: str,
    index: int,
    *,
    now: float | None = None,
) -> str:
    expires_at = int(time.time() if now is None else now) + POST_IMAGE_URL_TTL_SECONDS
    signature = sign_internal(_PURPOSE, _payload(post_id, index, expires_at))
    query = urlencode({"exp": expires_at, "sig": signature})
    return f"{api_origin.rstrip('/')}/posts/{post_id}/images/{index}?{query}"


def valid_post_image_signature(
    post_id: str,
    index: int,
    expires_at: str | None,
    signature: str | None,
    *,
    now: float | None = None,
) -> bool:
    try:
        expiry = int(expires_at or "")
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    if expiry < current or expiry > current + POST_IMAGE_URL_TTL_SECONDS + 60:
        return False
    return verify_internal(_PURPOSE, _payload(post_id, index, expiry), signature or "")
