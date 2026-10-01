"""The background collector.

Wakes hourly, asks whether today has already been collected, and goes back to
sleep if it has. There is no cron and no scheduler: a container that was down at
04:00 catches up the moment it comes back, which is the failure a fixed daily
schedule handles worst.

The loop must not be able to die. Three rules enforce that:

1. The try around the body catches Exception, never BaseException, so
   asyncio.CancelledError still propagates and shutdown stays clean.
2. The sleep is outside the try, so a persistent failure retries in an hour
   instead of spinning.
3. Every attempt is recorded in collection_runs whether it worked or not, so a
   failure that somehow escapes notification is still visible on the web page.

Notifications are the fourth rule, and the important one. A loop that survives
forever while quietly collecting nothing is worse than one that crashes.

Accounts marked notify also get their balance sent after each day's first
successful collection, which is the only time there is a new number to report.
"""

import argparse
import asyncio
import datetime as dt
import logging
import sqlite3
from decimal import Decimal

from wima import collect, config, db, notify
from wima.ebanking import EnableBanking, SessionNotAuthorized

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 3600

# A single failed morning is not worth waking you up for, since the next tick is
# an hour away. More than a day without a success means something is actually wrong.
STALE_AFTER_HOURS = 26

# Consent lasts 180 days but history only reaches back 90, so a lapse is only
# recoverable for three months. Nag early.
EXPIRY_WARNING_DAYS = 7

NOTIFY_INTERVAL_HOURS = 24

# The available balance, which is what you can actually spend. Backfill only
# reconstructs ITBD, so earlier ITAV readings exist only for days actually observed.
BALANCE_TYPE = "ITAV"

MINUS = "\u2212"


def auth_link(settings: config.Settings) -> str:
    """The sentence to append to a notification, when there is somewhere to send you."""
    return f"\n\n{settings.public_url}/auth" if settings.public_url else ""


def start_of_local_day() -> str:
    """Midnight in your timezone, as UTC, since that is how runs are stored."""
    midnight = dt.datetime.combine(dt.date.today(), dt.time.min).astimezone()
    return midnight.astimezone(dt.UTC).isoformat()


async def warn_about_expiry(
    connection: sqlite3.Connection, settings: config.Settings
) -> None:
    """Remind before the consent dies, using the stored valid_until, no request."""
    row = db.get_session(connection)
    if row is None:
        return

    remaining = dt.datetime.fromisoformat(row["valid_until"]) - dt.datetime.now(dt.UTC)
    if remaining > dt.timedelta(days=EXPIRY_WARNING_DAYS):
        return

    days = max(0, remaining.days)
    await notify.send_once(
        connection,
        settings,
        kind="consent-expiring",
        min_interval_hours=NOTIFY_INTERVAL_HOURS,
        title="Bank access expires soon",
        message=(
            f"{settings.aspsp_name} consent expires in {days} days. Re-authorize to "
            f"keep collecting. History only reaches back 90 days, so a longer lapse "
            f"than that loses data permanently." + auth_link(settings)
        ),
        tags="warning",
    )


async def warn_about_staleness(
    connection: sqlite3.Connection, settings: config.Settings, error: Exception
) -> None:
    """Only complain once collection has been broken for more than a day."""
    last = db.last_successful_run(connection)
    if last is not None:
        age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(last["started_at"])
        if age < dt.timedelta(hours=STALE_AFTER_HOURS):
            logger.info("collection failed but last success was %s ago, not notifying", age)
            return

    await notify.send_once(
        connection,
        settings,
        kind="collection-failing",
        min_interval_hours=NOTIFY_INTERVAL_HOURS,
        title="Balance collection is failing",
        message=f"No successful collection in over {STALE_AFTER_HOURS} hours.\n\n{error}",
        tags="rotating_light",
    )


def format_amount(amount: Decimal, signed: bool = False) -> str:
    """12 345.67, with a real minus sign, and a plus too when signed."""
    digits = f"{abs(amount):,.2f}".replace(",", " ")
    if amount < 0:
        return MINUS + digits
    if signed and amount > 0:
        return "+" + digits
    return digits


def balance_message(latest: sqlite3.Row, previous: sqlite3.Row | None) -> str:
    """The balance, and how it moved since the previous reading if comparable."""
    amount = Decimal(latest["amount"])
    text = f"{format_amount(amount)} {latest['currency']}"
    # A change across currencies is meaningless, so it is left out rather than guessed.
    if previous is None or previous["currency"] != latest["currency"]:
        return text

    latest_date = dt.date.fromisoformat(latest["reference_date"])
    previous_date = dt.date.fromisoformat(previous["reference_date"])
    since = (
        "yesterday"
        if latest_date - previous_date == dt.timedelta(days=1)
        else previous_date.isoformat()
    )
    change = amount - Decimal(previous["amount"])
    if change == 0:
        return f"{text} (unchanged since {since})"
    return f"{text} ({format_amount(change, signed=True)} since {since})"


async def notify_balance(
    connection: sqlite3.Connection, account: config.Account, settings: config.Settings
) -> None:
    """Send one account's balance. send logs its own failures; there is no retry."""
    rows = db.recent_balances(connection, account.hash, BALANCE_TYPE, limit=2)
    if not rows or rows[0]["fetched_at"] < start_of_local_day():
        logger.warning(
            "no fresh %s balance for %s today, not sending", BALANCE_TYPE, account.label
        )
        return
    previous = rows[1] if len(rows) > 1 else None
    await notify.send(settings, account.label, balance_message(rows[0], previous))


async def notify_balances(
    connection: sqlite3.Connection,
    accounts: list[config.Account],
    settings: config.Settings,
) -> None:
    """One message per notify account. A problem with one must not silence the rest."""
    for account in accounts:
        if not account.notify:
            continue
        try:
            await notify_balance(connection, account, settings)
        except Exception:
            logger.exception("could not send balance for %s", account.label)


async def tick(
    connection: sqlite3.Connection,
    client: EnableBanking,
    accounts: list[config.Account],
    settings: config.Settings,
) -> None:
    """One pass. Handles its own errors so run_forever's catch is a backstop."""
    await warn_about_expiry(connection, settings)

    if db.has_successful_run_since(connection, start_of_local_day()):
        logger.info("already collected today, nothing to do")
        return

    try:
        written = await collect.run(connection, client, accounts)
        logger.info("collected %d rows", written)
    except SessionNotAuthorized as error:
        logger.error("session is %s, re-authorization needed", error.status)
        await notify.send_once(
            connection,
            settings,
            kind="session-expired",
            min_interval_hours=NOTIFY_INTERVAL_HOURS,
            title="Bank access needs re-authorization",
            message=(
                f"The {settings.aspsp_name} session is {error.status} and collection "
                f"has stopped. Re-authorize to resume. Gaps shorter than 90 days are "
                f"backfilled automatically once access is restored." + auth_link(settings)
            ),
            tags="lock",
        )
    except Exception as error:
        logger.exception("collection failed")
        await warn_about_staleness(connection, settings, error)
    else:
        # Outside the try, so a bug in here cannot be mistaken for a failed
        # collection and set off the staleness warning.
        await notify_balances(connection, accounts, settings)


async def run_forever(
    connection: sqlite3.Connection,
    client: EnableBanking,
    accounts: list[config.Account],
    settings: config.Settings,
) -> None:
    logger.info("collector started, waking every %d seconds", INTERVAL_SECONDS)
    while True:
        try:
            await tick(connection, client, accounts, settings)
        except Exception:
            # tick handles its own failures, so reaching here means a bug in the
            # handling itself. Log it and keep the loop alive regardless.
            logger.exception("tick raised, continuing anyway")
        await asyncio.sleep(INTERVAL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Run the collector loop")
    parser.add_argument(
        "--once", action="store_true", help="run a single tick and exit, for testing"
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="send one notification now, bypassing the rate limit, and exit",
    )
    args = parser.parse_args()

    if args.test_notification:
        settings = config.load_settings()
        if not settings.ntfy_topic:
            raise SystemExit("NTFY_TOPIC is not set, nothing would be sent")
        sent = asyncio.run(
            notify.send(
                settings,
                "Test from whats-in-my-account",
                "If you are reading this, notifications work.",
                tags="white_check_mark",
            )
        )
        raise SystemExit(0 if sent else "send failed, see the log above")

    settings = config.load_settings()
    connection = db.connect(settings.db_path)
    client = EnableBanking(settings)
    accounts = config.load_accounts()

    coroutine = (
        tick(connection, client, accounts, settings)
        if args.once
        else run_forever(connection, client, accounts, settings)
    )
    try:
        asyncio.run(coroutine)
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
