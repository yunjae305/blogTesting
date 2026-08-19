from app.posting.url_safety import safe_url_for_log


def test_safe_url_for_log_never_exposes_path_query_fragment_or_userinfo():
    secret_parts = ("writer@example.com", "oauth-code", "access-token", "fragment-secret")
    raw = (
        "https://writer%40example.com:password@www.threads.net/"
        "writer@example.com/checkpoint?code=oauth-code&token=access-token#fragment-secret"
    )

    rendered = safe_url_for_log(raw)

    assert rendered.startswith("https://www.threads.net/")
    assert all(secret not in rendered for secret in secret_parts)


def test_safe_url_for_log_rejects_non_http_values_without_echoing_them():
    secret = "not-a-url-with-password"

    assert safe_url_for_log(secret) == "(redacted)"
    assert secret not in safe_url_for_log(secret)
