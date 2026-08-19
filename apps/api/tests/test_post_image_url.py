from urllib.parse import parse_qs, urlparse

from app.posting.image_url import signed_post_image_url, valid_post_image_signature


def _parts(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    return query["exp"][0], query["sig"][0]


def test_signed_post_image_url_is_short_lived_and_bound_to_path(monkeypatch):
    from app.modules.auth import token

    monkeypatch.setattr(token, "_SECRET", b"s" * 32)
    url = signed_post_image_url("https://blog.example", "post_1", 2, now=1_000)
    expiry, signature = _parts(url)

    assert valid_post_image_signature("post_1", 2, expiry, signature, now=1_100)
    assert not valid_post_image_signature("post_2", 2, expiry, signature, now=1_100)
    assert not valid_post_image_signature("post_1", 3, expiry, signature, now=1_100)
    assert not valid_post_image_signature("post_1", 2, expiry, signature, now=1_601)
    assert not valid_post_image_signature("post_1", 2, expiry, signature + "x", now=1_100)
