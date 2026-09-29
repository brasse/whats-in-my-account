"""Reconstruct past daily balances from transaction history.

Swedbank does not populate balance_after_transaction, so past balances are
arithmetic rather than lookup:

    balance(D) = balance(D + 1) - sum of booked transactions on day D + 1

Walking backwards from today's observed booked balance gives a closing balance
for every day in the window. Pending transactions are excluded because they move
the available balance and not the booked one, so only ITBD can be reconstructed.
There is no historical ITAV and inventing one would be a lie.

Transactions are consumed and discarded, never stored. Swedbank leaves both
transaction_id and entry_reference null, so they cannot be deduplicated across
fetches, and storing them would mean storing duplicates.

The history window is a rolling 90 days, the PSD2 minimum. Gaps longer than that
cannot be healed, ever.
"""

import argparse
import asyncio
import datetime as dt
import logging
import sqlite3
from decimal import Decimal

from wima import config, db
from wima.ebanking import EnableBanking, EnableBankingError

logger = logging.getLogger(__name__)

# How far back the bank will serve transactions. Asking for more returns 422.
HISTORY_DAYS = 90

# The balance type the reconstruction produces. Booked, not available.
DERIVED_TYPE = "ITBD"

CENTS = Decimal("0.01")


def signed_amount(transaction: dict) -> Decimal:
    """The sign lives in credit_debit_indicator, not in the amount."""
    amount = Decimal(transaction["transaction_amount"]["amount"])
    return -amount if transaction["credit_debit_indicator"] == "DBIT" else amount


async def fetch_transactions(
    client: EnableBanking, uid: str, date_from: dt.date, max_pages: int = 50
) -> list[dict]:
    """Follow continuation keys to the end.

    An empty page is not the end. The first response is reliably empty with a
    key attached, and the last page holds the pending transactions.
    """
    transactions: list[dict] = []
    continuation_key = None

    for page in range(1, max_pages + 1):
        body = await client.get_transactions(
            uid, date_from.isoformat(), continuation_key
        )
        page_transactions = body.get("transactions", [])
        transactions.extend(page_transactions)
        logger.debug("page %d: %d transactions", page, len(page_transactions))

        continuation_key = body.get("continuation_key")
        if not continuation_key:
            return transactions

    logger.warning("stopped following continuation keys at %d pages", max_pages)
    return transactions


async def fetch_transactions_from_earliest(
    client: EnableBanking, uid: str, date_from: dt.date
) -> tuple[list[dict], dt.date]:
    """Ask for date_from, stepping forward a day at a time while the bank says 422.

    The window boundary moves daily and their idea of it is a day or two off ours,
    so rather than computing it exactly we ask and accept being told no.
    """
    for offset in range(5):
        attempt = date_from + dt.timedelta(days=offset)
        try:
            return await fetch_transactions(client, uid, attempt), attempt
        except EnableBankingError as error:
            if error.status != 422:
                raise
            logger.info("%s is outside the history window, trying a day later", attempt)
    raise EnableBankingError("GET", "transactions", 422, "no acceptable date_from found")


def reconstruct(
    anchor_date: dt.date,
    anchor_amount: Decimal,
    transactions: list[dict],
    earliest: dt.date,
) -> dict[dt.date, Decimal]:
    """Closing balances for every day in [earliest, anchor_date), walking back.

    anchor_date itself is excluded, since that balance is observed rather than
    derived and the caller already has it.
    """
    by_day: dict[dt.date, Decimal] = {}
    for transaction in transactions:
        if transaction.get("status") != "BOOK":
            continue
        booking_date = transaction.get("booking_date")
        if not booking_date:
            continue
        day = dt.date.fromisoformat(booking_date)
        if day > anchor_date:
            continue
        by_day[day] = by_day.get(day, Decimal(0)) + signed_amount(transaction)

    balances: dict[dt.date, Decimal] = {}
    running = anchor_amount
    day = anchor_date
    while day > earliest:
        # Undo the day we are standing on to land on the previous day's close.
        running -= by_day.get(day, Decimal(0))
        day -= dt.timedelta(days=1)
        balances[day] = running.quantize(CENTS)

    return balances


def observed_booked_balance(balances: list[dict]) -> tuple[dt.date, Decimal, str]:
    for balance in balances:
        if balance["balance_type"] == DERIVED_TYPE:
            return (
                dt.date.fromisoformat(balance["reference_date"]),
                Decimal(balance["balance_amount"]["amount"]),
                balance["balance_amount"]["currency"],
            )
    raise ValueError(f"no {DERIVED_TYPE} balance in the response to anchor against")


async def backfill_account(
    connection: sqlite3.Connection,
    client: EnableBanking,
    account: config.Account,
    uid: str,
    since: dt.date,
) -> int:
    """Fill derived balances for [since, today] on one account."""
    anchor_date, anchor_amount, currency = observed_booked_balance(
        await client.get_balances(uid)
    )

    # The anchor is a real observation, so it is worth keeping on its own account.
    db.record_balance(
        connection,
        account_hash=account.hash,
        reference_date=anchor_date.isoformat(),
        balance_type=DERIVED_TYPE,
        amount=str(anchor_amount),
        currency=currency,
        source="observed",
    )

    transactions, actual_from = await fetch_transactions_from_earliest(
        client, uid, since
    )
    derived = reconstruct(anchor_date, anchor_amount, transactions, actual_from)

    for day, amount in sorted(derived.items()):
        db.record_balance(
            connection,
            account_hash=account.hash,
            reference_date=day.isoformat(),
            balance_type=DERIVED_TYPE,
            amount=str(amount),
            currency=currency,
            source="derived",
        )

    logger.info(
        "%s: %d derived days from %s to %s, anchored on %s at %s",
        account.label,
        len(derived),
        actual_from,
        anchor_date - dt.timedelta(days=1),
        anchor_date,
        anchor_amount,
    )
    return len(derived)


def gap_start(connection: sqlite3.Connection, account_hash: str) -> dt.date | None:
    """Earliest day needing reconstruction, or None if the history is contiguous.

    Must be called before today's balance is written, otherwise the latest stored
    date is always today and no gap is ever visible.
    """
    today = dt.date.today()
    window_start = today - dt.timedelta(days=HISTORY_DAYS)
    latest = db.latest_balance_date(connection, account_hash)

    if latest is None:
        return window_start

    since = max(dt.date.fromisoformat(latest), window_start)
    if since >= today - dt.timedelta(days=1):
        return None
    return since


async def run(
    connection: sqlite3.Connection, client: EnableBanking, accounts: list[config.Account], days: int
) -> int:
    uid_by_hash = await client.uid_by_hash(db.require_session_id(connection))

    since = dt.date.today() - dt.timedelta(days=days)
    total = 0
    for account in accounts:
        uid = uid_by_hash.get(account.hash)
        if uid is None:
            logger.warning("%s is not in the current session, skipping", account.label)
            continue
        total += await backfill_account(connection, client, account, uid, since)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Reconstruct past balances")
    parser.add_argument(
        "--days",
        type=int,
        default=HISTORY_DAYS,
        help=f"how far back to reconstruct, at most {HISTORY_DAYS}",
    )
    args = parser.parse_args()

    settings = config.load_settings()
    connection = db.connect(settings.db_path)
    client = EnableBanking(settings)

    written = asyncio.run(run(connection, client, config.load_accounts(), args.days))
    print(f"wrote {written} derived rows")


if __name__ == "__main__":
    main()
