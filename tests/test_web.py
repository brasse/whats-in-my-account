"""Run with: uv run python tests/test_web.py

Covers what can be checked without a bank: the login wall, the authorization
flow's guards, and the API shape the page depends on. Anything that would
actually talk to Enable Banking is out of scope.
"""

import logging
import os
import pathlib
import sys
import tempfile
import warnings

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

# The app starts its collector on startup, which fails loudly against a fixture
# database with no bank session. That failure is the loop behaving correctly,
# but its tracebacks bury the test results.
logging.disable(logging.CRITICAL)

from wima.auth import COOKIE_NAME, hash_password

USERNAME = "tester"
PASSWORD = "a-good-enough-password"

SCRATCH = pathlib.Path(tempfile.mkdtemp())
(SCRATCH / "key.pem").write_text("not-a-real-key")
(SCRATCH / "config.toml").write_text(
    f'[auth]\nusername = "{USERNAME}"\npassword_hash = "{hash_password(PASSWORD)}"\n'
    f'\n[[account]]\nhash = "abc"\nlabel = "Test account"\n'
)

os.environ.update(
    {
        "EB_APP_ID": "test",
        "EB_KEY_PATH": str(SCRATCH / "key.pem"),
        "WIMA_DB": str(SCRATCH / "test.db"),
        "WIMA_CONFIG": str(SCRATCH / "config.toml"),
    }
)

from fastapi.testclient import TestClient

from wima import web


def client() -> TestClient:
    return TestClient(web.app)


def logged_in(c: TestClient) -> TestClient:
    response = c.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.status_code
    assert c.cookies.get(COOKIE_NAME), "no session cookie was set"
    return c


# ---------------------------------------------------------------- the login wall


def test_page_redirects_to_login_when_signed_out() -> None:
    with client() as c:
        response = c.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_api_returns_401_not_a_login_page() -> None:
    """The page's fetch calls need a status, not HTML in a JSON parser."""
    with client() as c:
        response = c.get("/api/status")
    assert response.status_code == 401
    assert response.json()["detail"] == "not authenticated"


def test_auth_endpoint_is_protected() -> None:
    """A stranger must not be able to start a bank authorization."""
    with client() as c:
        response = c.get("/auth", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_health_and_static_are_public() -> None:
    with client() as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/static/chart.umd.min.js").status_code == 200


def test_wrong_password_is_refused() -> None:
    with client() as c:
        response = c.post("/login", data={"username": USERNAME, "password": "wrong"})
        assert response.status_code == 401
        assert "Wrong username or password" in response.text
        assert not c.cookies.get(COOKIE_NAME)


def test_wrong_username_is_refused() -> None:
    with client() as c:
        response = c.post("/login", data={"username": "someone", "password": PASSWORD})
        assert response.status_code == 401
        assert not c.cookies.get(COOKIE_NAME)


def test_login_then_logout() -> None:
    with client() as c:
        logged_in(c)
        assert c.get("/api/status").status_code == 200

        c.post("/logout")
        assert c.get("/api/status").status_code == 401, "logout did not end the session"


def test_deep_link_survives_login() -> None:
    """The ntfy notification points at /auth, which needs to work signed out."""
    with client() as c:
        redirect = c.get("/auth", follow_redirects=False)
        assert "next=%2Fauth" in redirect.headers["location"]

        form = c.get(redirect.headers["location"])
        assert 'value="/auth"' in form.text


def test_next_cannot_be_an_open_redirect() -> None:
    for hostile in ["//evil.example", "https://evil.example", "\\\\evil.example"]:
        assert web.safe_next(hostile) == "/", hostile
    assert web.safe_next("/auth") == "/auth"


# ------------------------------------------------------------------- the API


def test_status_shape() -> None:
    with client() as c:
        body = logged_in(c).get("/api/status").json()
    for key in [
        "last_success", "hours_since_success", "stale", "last_error",
        "consent_days_left", "consent_valid_until", "can_reauthorize", "commit",
    ]:
        assert key in body, key
    assert body["stale"] is True, "a database with no runs must read as stale"


def test_status_shows_a_short_commit() -> None:
    os.environ["WIMA_COMMIT"] = "50a4c73e0123456789abcdef0123456789abcdef"
    try:
        with client() as c:
            body = logged_in(c).get("/api/status").json()
    finally:
        del os.environ["WIMA_COMMIT"]
    assert body["commit"] == "50a4c73e", body["commit"]


def test_status_without_a_commit() -> None:
    with client() as c:
        body = logged_in(c).get("/api/status").json()
    assert body["commit"] is None, body["commit"]


def test_balances_shape_with_no_data() -> None:
    with client() as c:
        body = logged_in(c).get("/api/balances?type=ITBD").json()
    assert body["type"] == "ITBD"
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["points"] == []
    assert body["accounts"][0]["label"] == "Test account"


def test_account_ids_are_short_and_stable() -> None:
    with client() as c:
        accounts = logged_in(c).get("/api/accounts").json()
    account_id = accounts[0]["id"]
    assert len(account_id) == 8 and account_id.isalnum()
    assert "abc" not in str(accounts), "the account hash must not leak into the API"


# ------------------------------------------------- the authorization flow's guards


def test_auth_refuses_without_a_public_url() -> None:
    os.environ.pop("WIMA_PUBLIC_URL", None)
    with client() as c:
        response = logged_in(c).get("/auth", follow_redirects=False)
    assert response.status_code == 500
    assert "WIMA_PUBLIC_URL" in response.json()["detail"]


def test_callback_rejects_unknown_state() -> None:
    with client() as c:
        response = logged_in(c).get("/auth/callback?code=x&state=forged")
    assert response.status_code == 400
    assert "state" in response.json()["detail"]


def test_callback_rejects_missing_parameters() -> None:
    with client() as c:
        signed_in = logged_in(c)
        assert signed_in.get("/auth/callback").status_code == 400
        assert signed_in.get("/auth/callback?code=x").status_code == 400
        assert signed_in.get("/auth/callback?error=access_denied").status_code == 400


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok  {name}")
    print("all passed")
