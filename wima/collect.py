"""Fetch today's balances and write them to the database.

The account uid is session-scoped and changes whenever the session is recreated,
so it is looked up fresh from the session on every run and never persisted. The
stable identifier is identification_hash, which is what accounts.toml names and
what the balances table is keyed on.
"""

import argparse
import asyncio
import json
import logging
import pathlib
import sqlite3

from wima import backfill, config, db
from wima.ebanking import EnableBanking

logger = logging.getLogger(__name__)


async def collect_once(
    connection: sqlite3.Connection,
    client: EnableBanking,
    accounts: list[config.Account],
) -> int:
    uid_by_hash = await client.uid_by_hash(db.require_session_id(connection))

    written = 0
    for account in accounts:
        uid = uid_by_hash.get(account.hash)
        if uid is None:
            logger.warning("%s is not in the current session, skipping", account.label)
            continue

        # Checked before today's rows land, or the gap becomes invisible.
        since = backfill.gap_start(connection, account.hash)

        for balance in await client.get_balances(uid):
            db.record_balance(
                connection,
                account_hash=account.hash,
                reference_date=balance["reference_date"],
                balance_type=balance["balance_type"],
                amount=balance["balance_amount"]["amount"],
                currency=balance["balance_amount"]["currency"],
            )
            written += 1
            logger.info(
                "%s %s %s %s %s",
                account.label,
                balance["reference_date"],
                balance["balance_type"],
                balance["balance_amount"]["amount"],
                balance["balance_amount"]["currency"],
            )

        if since is not None:
            written += await backfill.backfill_account(
                connection, client, account, uid, since
            )

    return written


async def run(
    connection: sqlite3.Connection,
    client: EnableBanking,
    accounts: list[config.Account],
) -> int:
    """One attempt, recorded in collection_runs whether it works or not."""
    run_id = db.start_run(connection)
    try:
        written = await collect_once(connection, client, accounts)
    except Exception as error:
        db.finish_run(connection, run_id, ok=False, error=f"{type(error).__name__}: {error}")
        raise
    db.finish_run(connection, run_id, ok=True)
    return written


def import_session(connection: sqlite3.Connection, path: pathlib.Path) -> None:
    """Seed the session table from an explore.py session dump."""
    body = json.loads(path.read_text())
    db.set_session(connection, body["session_id"], body["access"]["valid_until"])
    print(f"stored session {body['session_id'][:8]}..., valid until {body['access']['valid_until']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--import-session",
        type=pathlib.Path,
        metavar="PATH",
        help="seed the session table from an explore.py session dump, then exit",
    )
    args = parser.parse_args()

    settings = config.load_settings()
    connection = db.connect(settings.db_path)

    if args.import_session:
        import_session(connection, args.import_session)
        return

    client = EnableBanking(settings)
    written = asyncio.run(run(connection, client, config.load_accounts()))
    print(f"wrote {written} rows")


if __name__ == "__main__":
    main()
