"""Run with: uv run python tests/test_web.py

Covers the parts of the web app that can be checked without a bank: the guards
on the authorization flow, and that the API shape the page depends on is stable.
Anything that would actually talk to Enable Banking is out of scope here.
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
# database with no session. That failure is the loop behaving correctly, but its
# tracebacks bury the test results, so keep them out of the output.
logging.disable(logging.CRITICAL)

SCRATCH = pathlib.Path(tempfile.mkdtemp())
(SCRATCH / "key.pem").write_text("not-a-real-key")
(SCRATCH / "accounts.toml").write_text(
    '[[account]]\nhash = "abc"\nlabel = "Test account"\n'
)

os.environ.update(
    {
        "EB_APP_ID": "test",
        "EB_KEY_PATH": str(SCRATCH / "key.pem"),
        "WIMA_DB": str(SCRATCH / "test.db"),
    }
)
os.chdir(SCRATCH)  # so load_accounts finds the fixture

from fastapi.testclient import TestClient

from wima import db, web


def client() -> TestClient:
    return TestClient(web.app)


def test_status_shape() -> None:
    with client() as c:
        body = c.get("/api/status").json()
    for key in [
        "last_success", "hours_since_success", "stale", "last_error",
        "consent_days_left", "consent_valid_until", "can_reauthorize",
    ]:
        assert key in body, key
    assert body["stale"] is True, "a database with no runs must read as stale"


def test_balances_shape_with_no_data() -> None:
    with client() as c:
        body = c.get("/api/balances?type=ITBD").json()
    assert body["type"] == "ITBD"
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["points"] == []
    assert body["accounts"][0]["label"] == "Test account"


def test_account_ids_are_short_and_stable() -> None:
    with client() as c:
        accounts = c.get("/api/accounts").json()
    account_id = accounts[0]["id"]
    assert len(account_id) == 8 and account_id.isalnum()
    # The hash itself must never leak into the API.
    assert "abc" not in str(accounts)


def test_auth_refuses_without_a_public_url() -> None:
    os.environ.pop("WIMA_PUBLIC_URL", None)
    with client() as c:
        response = c.get("/auth", follow_redirects=False)
    assert response.status_code == 500
    assert "WIMA_PUBLIC_URL" in response.json()["detail"]


def test_callback_rejects_unknown_state() -> None:
    with client() as c:
        response = c.get("/auth/callback?code=x&state=forged", follow_redirects=False)
    assert response.status_code == 400
    assert "state" in response.json()["detail"]


def test_callback_rejects_missing_parameters() -> None:
    with client() as c:
        assert c.get("/auth/callback").status_code == 400
        assert c.get("/auth/callback?code=x").status_code == 400
        assert c.get("/auth/callback?error=access_denied").status_code == 400


def test_static_chart_is_served() -> None:
    with client() as c:
        response = c.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert b"Chart.js" in response.content[:200]


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok  {name}")
    print("all passed")
