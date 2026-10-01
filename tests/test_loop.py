"""Run with: uv run python tests/test_loop.py

Covers the decisions the loop makes without touching the network: when to warn
about expiry, when to stay quiet about a single failed attempt, that the
rate limiting actually limits, and what the daily balance message says.
"""

import asyncio
import dataclasses
import datetime as dt
import pathlib
import sqlite3
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wima import collect, config, db, loop, notify

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
    """Stands in for notify.send, recording what would have been sent."""

    def __init__(self, succeeds: bool = True) -> None:
        self.titles: list[str] = []
        self.messages: list[str] = []
        self.succeeds = succeeds

    async def __call__(self, settings, title, message, tags="") -> bool:
        self.titles.append(title)
        self.messages.append(message)
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


CHECKING = config.Account(hash="checking", label="Privatkonto 1234", notify=True)
SAVINGS = config.Account(hash="savings", label="Sparkonto 5678", notify=False)


def balance(
    connection: sqlite3.Connection,
    account: config.Account,
    reference_date: str,
    amount: str,
    balance_type: str = loop.BALANCE_TYPE,
    currency: str = "SEK",
) -> None:
    db.record_balance(
        connection, account.hash, reference_date, balance_type, amount, currency
    )


def message_for(connection: sqlite3.Connection, account: config.Account) -> str:
    sender = FakeSender()
    install(sender)
    asyncio.run(loop.notify_balance(connection, account, SETTINGS))
    assert sender.titles == [account.label], sender.titles
    return sender.messages[0]


def test_balance_message_with_change() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-29", "12777.77")
    balance(connection, CHECKING, "2026-09-30", "12345.67")

    message = message_for(connection, CHECKING)
    assert message == "12 345.67 SEK (\u2212432.10 since yesterday)", message


def test_balance_message_after_a_gap_names_the_date() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-28", "100.00")
    balance(connection, CHECKING, "2026-09-30", "150.5")

    message = message_for(connection, CHECKING)
    assert message == "150.50 SEK (+50.50 since 2026-09-28)", message


def test_balance_message_unchanged() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-29", "100.00")
    balance(connection, CHECKING, "2026-09-30", "100")
    assert message_for(connection, CHECKING) == "100.00 SEK (unchanged since yesterday)"

    balance(connection, CHECKING, "2026-10-03", "100")
    message = message_for(connection, CHECKING)
    assert message == "100.00 SEK (unchanged since 2026-09-30)", message


def test_balance_message_without_itav_history_omits_change() -> None:
    """Backfill only derives ITBD, so the first ITAV reading has nothing to compare to."""
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-28", "1.00", balance_type="ITBD")
    balance(connection, CHECKING, "2026-09-29", "2.00", balance_type="ITBD")
    balance(connection, CHECKING, "2026-09-30", "1234567.8")

    message = message_for(connection, CHECKING)
    assert message == "1 234 567.80 SEK", message


def test_balance_message_omits_change_across_currencies() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-29", "10.00", currency="EUR")
    balance(connection, CHECKING, "2026-09-30", "100.00")
    assert message_for(connection, CHECKING) == "100.00 SEK"


def test_format_amount() -> None:
    assert loop.format_amount(Decimal("-1234.5")) == "\u22121 234.50"
    assert loop.format_amount(Decimal("1234.5")) == "1 234.50"
    assert loop.format_amount(Decimal("1234.5"), signed=True) == "+1 234.50"
    assert loop.format_amount(Decimal("-0.5"), signed=True) == "\u22120.50"
    assert loop.format_amount(Decimal(0), signed=True) == "0.00"


def test_negative_balance_message() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-29", "50.00")
    balance(connection, CHECKING, "2026-09-30", "-25.00")

    message = message_for(connection, CHECKING)
    assert message == "\u221225.00 SEK (\u221275.00 since yesterday)", message


def test_only_notify_accounts_are_sent() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-30", "1.00")
    balance(connection, SAVINGS, "2026-09-30", "2.00")
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.notify_balances(connection, [CHECKING, SAVINGS], SETTINGS))
    assert sender.titles == [CHECKING.label], sender.titles


def test_one_failing_account_does_not_stop_the_others() -> None:
    connection = fresh_db()
    broken = config.Account(hash="broken", label="Broken", notify=True)
    balance(connection, broken, "2026-09-30", "not a number")
    balance(connection, CHECKING, "2026-09-30", "1.00")
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.notify_balances(connection, [broken, CHECKING], SETTINGS))
    assert sender.titles == [CHECKING.label], sender.titles


def test_notify_account_without_balance_sends_nothing() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-30", "1.00", balance_type="ITBD")
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.notify_balances(connection, [CHECKING], SETTINGS))
    assert sender.titles == [], sender.titles


def test_stale_balance_is_not_sent() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-29", "1.00")
    connection.execute("UPDATE balances SET fetched_at = ?", (ago(days=1),))
    sender = FakeSender()
    install(sender)

    asyncio.run(loop.notify_balances(connection, [CHECKING], SETTINGS))
    assert sender.titles == [], sender.titles


def test_tick_sends_balances_after_successful_collection() -> None:
    connection = fresh_db()
    sender = FakeSender()
    install(sender)

    async def fake_run(connection, client, accounts) -> int:
        balance(connection, CHECKING, "2026-09-30", "42.00")
        return 1

    collect.run = fake_run
    asyncio.run(loop.tick(connection, None, [CHECKING], SETTINGS))
    assert sender.titles == [CHECKING.label], sender.titles
    assert sender.messages == ["42.00 SEK"], sender.messages


def test_tick_sends_no_balances_when_collection_fails() -> None:
    connection = fresh_db()
    balance(connection, CHECKING, "2026-09-30", "42.00")
    sender = FakeSender()
    install(sender)

    async def failing_run(connection, client, accounts) -> int:
        raise RuntimeError("bank is down")

    collect.run = failing_run
    asyncio.run(loop.tick(connection, None, [CHECKING], SETTINGS))
    # The staleness alert may legitimately fire here; the balance must not.
    assert CHECKING.label not in sender.titles, sender.titles


if __name__ == "__main__":
    real_send = notify.send
    real_run = collect.run
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            notify.send = real_send
            collect.run = real_run
            test()
            print(f"ok  {name}")
    print("all passed")
