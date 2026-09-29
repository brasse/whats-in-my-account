"""Run with: uv run python tests/test_auth.py

Password hashing and login sessions.
"""

import datetime as dt
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wima import auth, db


def fresh_db():
    return db.connect(pathlib.Path(tempfile.mkdtemp()) / "t.db")


def test_hash_and_verify() -> None:
    encoded = auth.hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert auth.verify_password("correct horse battery staple", encoded)
    assert not auth.verify_password("Correct horse battery staple", encoded)
    assert not auth.verify_password("", encoded)


def test_same_password_hashes_differently() -> None:
    """A random salt per hash, so equal passwords are not visibly equal."""
    assert auth.hash_password("hunter2hunter2") != auth.hash_password("hunter2hunter2")


def test_malformed_hash_is_rejected_not_crashed() -> None:
    for bad in ["", "nonsense", "scrypt$oops", "bcrypt$1$2$3$4$5", "scrypt$a$b$c$d$e"]:
        assert auth.verify_password("whatever", bad) is False, bad


def test_session_round_trip() -> None:
    connection = fresh_db()
    token = auth.start_session(connection)

    assert auth.session_is_valid(connection, token)
    assert not auth.session_is_valid(connection, "some-other-token")

    auth.end_session(connection, token)
    assert not auth.session_is_valid(connection, token)


def test_raw_token_is_never_stored() -> None:
    """A stolen database must not yield a working cookie."""
    connection = fresh_db()
    token = auth.start_session(connection)

    stored = [
        row["token_digest"]
        for row in connection.execute("SELECT token_digest FROM web_sessions")
    ]
    assert token not in stored
    assert stored == [auth.token_digest(token)]


def test_expired_session_is_rejected_and_reaped() -> None:
    connection = fresh_db()
    token = "expired-token"
    yesterday = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).isoformat()
    db.create_web_session(connection, auth.token_digest(token), yesterday)

    assert not auth.session_is_valid(connection, token)
    remaining = connection.execute("SELECT COUNT(*) c FROM web_sessions").fetchone()["c"]
    assert remaining == 0, "expired sessions should not accumulate"


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok  {name}")
    print("all passed")
