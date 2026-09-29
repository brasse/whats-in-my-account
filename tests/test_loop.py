"""Run with: uv run python tests/test_loop.py

Covers the decisions the loop makes without touching the network: when to warn
about expiry, when to stay quiet about a single failed attempt, and that the
rate limiting actually limits.
"""

import asyncio
import dataclasses
import datetime as dt
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wima import config, db, loop, notify

SETTINGS = config.Settings(
    app_id="x",
    private_key="x",
    psu_ip="127.0.0.1",
    psu_user_agent="x",
    db_path=pathlib.Path("unused"),
    ntfy_url="https://ntfy.example",
    ntfy_topic="test-topic",
    aspsp_name="Swedbank",
    aspsp_country="SE",
    consent_days=180,
    public_url="https://what.example",
)


class FakeSender:
    """Stands in for notify.send, recording titles instead of sending them."""

    def __init__(self, succeeds: bool = True) -> None:
        self.titles: list[str] = []
        self.succeeds = succeeds

    async def __call__(self, settings, title, message, tags="") -> bool:
        self.titles.append(title)
        return self.succeeds


def fresh_db() -> sqlite3.Connection:
    return db.connect(pathlib.Path(tempfile.mkdtemp()) / "t.db")


def install(sender: FakeSender) -> None:
    notify.send = sender


def ago(**kwargs) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(**kwargs)).isoformat()


def ahead(**kwargs) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(**kwargs)).isoformat()


def test_expiry_warning_fires_when_close() -> None:
    connection = fresh_db()
    db.set_session(connection, "s", ahead(days=3))
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.warn_about_expiry(connection, SETTINGS))
    assert sender.titles == ["Bank access expires soon"], sender.titles

    # Second call within the rate limit window stays quiet.
    asyncio.run(loop.warn_about_expiry(connection, SETTINGS))
    assert len(sender.titles) == 1, sender.titles


def test_expiry_warning_silent_when_far_off() -> None:
    connection = fresh_db()
    db.set_session(connection, "s", ahead(days=179))
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.warn_about_expiry(connection, SETTINGS))
    assert sender.titles == []


def test_failed_send_is_retried() -> None:
    """A send that fails must not be recorded as delivered."""
    connection = fresh_db()
    db.set_session(connection, "s", ahead(days=3))

    failing = FakeSender(succeeds=False)
    install(failing)
    asyncio.run(loop.warn_about_expiry(connection, SETTINGS))
    assert len(failing.titles) == 1

    working = FakeSender()
    install(working)
    asyncio.run(loop.warn_about_expiry(connection, SETTINGS))
    assert len(working.titles) == 1, "failed send was wrongly recorded as sent"


def test_one_bad_morning_is_not_an_alert() -> None:
    connection = fresh_db()
    connection.execute(
        "INSERT INTO collection_runs (started_at, finished_at, ok) VALUES (?, ?, 1)",
        (ago(hours=3), ago(hours=3)),
    )
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.warn_about_staleness(connection, SETTINGS, RuntimeError("boom")))
    assert sender.titles == [], "alerted on a single transient failure"


def test_a_day_without_success_is_an_alert() -> None:
    connection = fresh_db()
    connection.execute(
        "INSERT INTO collection_runs (started_at, finished_at, ok) VALUES (?, ?, 1)",
        (ago(hours=30), ago(hours=30)),
    )
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.warn_about_staleness(connection, SETTINGS, RuntimeError("boom")))
    assert sender.titles == ["Balance collection is failing"], sender.titles


def test_never_collected_is_an_alert() -> None:
    connection = fresh_db()
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.warn_about_staleness(connection, SETTINGS, RuntimeError("boom")))
    assert sender.titles == ["Balance collection is failing"], sender.titles


def test_tick_skips_when_today_is_done() -> None:
    connection = fresh_db()
    connection.execute(
        "INSERT INTO collection_runs (started_at, finished_at, ok) VALUES (?, ?, 1)",
        (db.now(), db.now()),
    )
    install(FakeSender())

    # client is None: reaching it at all would raise, which is the assertion.
    asyncio.run(loop.tick(connection, None, [], SETTINGS))


def test_notifications_are_disabled_without_a_topic() -> None:
    settings = dataclasses.replace(SETTINGS, ntfy_topic=None)
    assert asyncio.run(notify.send(settings, "t", "m")) is False


def test_auth_link_is_omitted_when_there_is_nowhere_to_send_you() -> None:
    assert loop.auth_link(SETTINGS).strip() == "https://what.example/auth"
    assert loop.auth_link(dataclasses.replace(SETTINGS, public_url=None)) == ""


if __name__ == "__main__":
    real_send = notify.send
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            notify.send = real_send
            test()
            print(f"ok  {name}")
    print("all passed")
