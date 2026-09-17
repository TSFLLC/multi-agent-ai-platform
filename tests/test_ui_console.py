"""Local Operator Console static shell — MA5-UI.

Covers app.web: the console's HTML is served at "/", the real local MA1B
token (never a placeholder, never a hard-coded value) is injected into it,
and static assets are reachable — without needing a browser.
"""


def test_console_index_served_without_auth(client, bootstrap):
    """The shell page itself carries no API data — only same-origin JS
    that will call the real, auth-protected API — so it is reachable
    without a Bearer token, exactly like any other static asset would be."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Operator Console" in resp.text


def test_console_index_injects_real_local_token(client, bootstrap, auth_token):
    resp = client.get("/")
    assert resp.status_code == 200
    assert auth_token in resp.text
    assert "%%MAP_LOCAL_TOKEN_VALUE%%" not in resp.text  # placeholder must be replaced, not left in
    assert "window.__MAP_LOCAL_TOKEN__" in resp.text  # the JS global name itself must stay intact


def test_console_static_assets_served(client, bootstrap):
    resp = client.get("/assets/js/main.js")
    assert resp.status_code == 200
    assert "registerRoute" in resp.text

    resp_css = client.get("/assets/css/app.css")
    assert resp_css.status_code == 200


def test_console_routes_never_shadow_the_api(client, bootstrap, auth_headers):
    """"/assets" and "/" are mounted last (app.main) specifically so they
    can never intercept a real resource route."""
    resp = client.get("/health")
    assert resp.status_code == 200
    resp = client.get("/me", headers=auth_headers)
    assert resp.status_code == 200
