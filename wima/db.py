"""SQLite storage.

Amounts are kept as the exact strings the bank returned. SQLite has no decimal
type, and floats cannot represent most decimal fractions exactly (0.1 + 0.2 is
0.30000000000000004), so parsing into Decimal is left to whoever actually needs
to do arithmetic.
"""

import datetime as dt
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS balances (
    account_hash   TEXT NOT NULL,
    reference_date TEXT NOT NULL,
    balance_type   TEXT NOT NULL,
    amount         TEXT NOT NULL,
    currency       TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    -- 'observed' came from the balances endpoint, 'derived' was reconstructed
    -- from transaction history. Observed is never overwritten by derived.
    source         TEXT NOT NULL DEFAULT 'observed',
    PRIMARY KEY (account_hash, reference_date, balance_type)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS collection_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT
);

-- When each kind of notification was last sent, so a stuck collector produces
-- one message a day rather than one an hour.
CREATE TABLE IF NOT EXISTS notifications (
    kind    TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- One row, ever. The check constraint says so out loud.
CREATE TABLE IF NOT EXISTS session (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    session_id  TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def connect(path: pathlib.Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(balances)")}
    if "source" not in columns:
        connection.execute(
            "ALTER TABLE balances ADD COLUMN source TEXT NOT NULL DEFAULT 'observed'"
        )


def set_session(connection: sqlite3.Connection, session_id: str, valid_until: str) -> None:
    connection.execute(
        """
        INSERT INTO session (id, session_id, valid_until, created_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            session_id = excluded.session_id,
            valid_until = excluded.valid_until,
            created_at = excluded.created_at
        """,
        (session_id, valid_until, now()),
    )


def get_session(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM session WHERE id = 1").fetchone()


class NoSession(Exception):
    """Nothing has ever been authorized, as opposed to an authorization going stale."""


def require_session_id(connection: sqlite3.Connection) -> str:
    row = get_session(connection)
    if row is None:
        raise NoSession("no session stored, authorize first")
    return row["session_id"]


def record_balance(
    connection: sqlite3.Connection,
    account_hash: str,
    reference_date: str,
    balance_type: str,
    amount: str,
    currency: str,
    source: str = "observed",
) -> None:
    """Upsert, so re-running on the same day corrects rather than duplicates.

    The WHERE clause is the whole point: an observed balance overwrites anything,
    a derived one only ever replaces another derived one. Reconstructed arithmetic
    must not clobber a number the bank actually told us.
    """
    connection.execute(
        """
        INSERT INTO balances
            (account_hash, reference_date, balance_type, amount, currency,
             fetched_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_hash, reference_date, balance_type) DO UPDATE SET
            amount = excluded.amount,
            currency = excluded.currency,
            fetched_at = excluded.fetched_at,
            source = excluded.source
        WHERE excluded.source = 'observed' OR balances.source = 'derived'
        """,
        (account_hash, reference_date, balance_type, amount, currency, now(), source),
    )


def latest_balance_date(connection: sqlite3.Connection, account_hash: str) -> str | None:
    """Most recent date we have any balance for, observed or derived."""
    row = connection.execute(
        "SELECT MAX(reference_date) AS d FROM balances WHERE account_hash = ?",
        (account_hash,),
    ).fetchone()
    return row["d"] if row else None


def balance_series(
    connection: sqlite3.Connection,
    account_hash: str,
    balance_type: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[sqlite3.Row]:
    """One account's balances of one type, oldest first."""
    clauses = ["account_hash = ?", "balance_type = ?"]
    parameters: list[str] = [account_hash, balance_type]
    if date_from:
        clauses.append("reference_date >= ?")
        parameters.append(date_from)
    if date_to:
        clauses.append("reference_date <= ?")
        parameters.append(date_to)

    return connection.execute(
        f"""
        SELECT reference_date, amount, currency, source
        FROM balances WHERE {" AND ".join(clauses)}
        ORDER BY reference_date
        """,
        parameters,
    ).fetchall()


def start_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO collection_runs (started_at, ok) VALUES (?, 0)", (now(),)
    )
    return cursor.lastrowid


def finish_run(
    connection: sqlite3.Connection, run_id: int, ok: bool, error: str | None = None
) -> None:
    connection.execute(
        "UPDATE collection_runs SET finished_at = ?, ok = ?, error = ? WHERE id = ?",
        (now(), 1 if ok else 0, error, run_id),
    )


def last_successful_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM collection_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def has_successful_run_since(connection: sqlite3.Connection, since: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM collection_runs WHERE ok = 1 AND started_at >= ? LIMIT 1",
        (since,),
    ).fetchone()
    return row is not None


def notification_sent_at(connection: sqlite3.Connection, kind: str) -> str | None:
    row = connection.execute(
        "SELECT sent_at FROM notifications WHERE kind = ?", (kind,)
    ).fetchone()
    return row["sent_at"] if row else None


def note_notification(connection: sqlite3.Connection, kind: str) -> None:
    connection.execute(
        """
        INSERT INTO notifications (kind, sent_at) VALUES (?, ?)
        ON CONFLICT (kind) DO UPDATE SET sent_at = excluded.sent_at
        """,
        (kind, now()),
    )
