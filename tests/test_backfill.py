"""Run with: uv run python tests/test_backfill.py

Plain asserts, no test framework, since there are two things worth checking and
neither needs fixtures: the reconstruction arithmetic, and the rule that a
derived balance never overwrites an observed one.
"""

import datetime as dt
import pathlib
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wima import db
from wima.backfill import reconstruct, signed_amount


def transaction(date: str, amount: str, indicator: str, status: str = "BOOK") -> dict:
    return {
        "transaction_amount": {"amount": amount, "currency": "SEK"},
        "credit_debit_indicator": indicator,
        "status": status,
        "booking_date": date,
    }


def test_signed_amount() -> None:
    assert signed_amount(transaction("2026-09-28", "10.00", "DBIT")) == Decimal("-10.00")
    assert signed_amount(transaction("2026-09-28", "10.00", "CRDT")) == Decimal("10.00")


def test_reconstruct() -> None:
    transactions = [
        transaction("2026-09-28", "100.00", "DBIT"),
        transaction("2026-09-27", "500.00", "CRDT"),
        transaction("2026-09-26", "50.00", "DBIT"),
        transaction("2026-09-26", "25.50", "DBIT"),
        # Pending moves the available balance, not the booked one.
        transaction("2026-09-25", "9000.00", "DBIT", status="PDNG"),
        # After the anchor, so it cannot have affected the anchor balance.
        transaction("2026-10-05", "1.00", "DBIT"),
    ]

    result = reconstruct(
        anchor_date=dt.date(2026, 9, 28),
        anchor_amount=Decimal("1000.00"),
        transactions=transactions,
        earliest=dt.date(2026, 9, 24),
    )

    assert result == {
        dt.date(2026, 9, 27): Decimal("1100.00"),  # undo a 100 debit
        dt.date(2026, 9, 26): Decimal("600.00"),   # undo a 500 credit
        dt.date(2026, 9, 25): Decimal("675.50"),   # undo 50 + 25.50 of debits
        dt.date(2026, 9, 24): Decimal("675.50"),   # quiet day, pending ignored
    }, result


def test_observed_survives_derived() -> None:
    path = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    connection = db.connect(path)

    def amount_now() -> tuple[str, str]:
        row = connection.execute(
            "SELECT amount, source FROM balances WHERE reference_date = '2026-09-28'"
        ).fetchone()
        return row["amount"], row["source"]

    db.record_balance(connection, "h", "2026-09-28", "ITBD", "100.00", "SEK", "observed")
    db.record_balance(connection, "h", "2026-09-28", "ITBD", "999.00", "SEK", "derived")
    assert amount_now() == ("100.00", "observed"), "derived clobbered observed"

    # A derived row may correct another derived row.
    db.record_balance(connection, "h", "2026-09-27", "ITBD", "1.00", "SEK", "derived")
    db.record_balance(connection, "h", "2026-09-27", "ITBD", "2.00", "SEK", "derived")
    row = connection.execute(
        "SELECT amount FROM balances WHERE reference_date = '2026-09-27'"
    ).fetchone()
    assert row["amount"] == "2.00", "derived failed to correct derived"

    # An observed row always wins, whatever was there before.
    db.record_balance(connection, "h", "2026-09-27", "ITBD", "3.00", "SEK", "observed")
    row = connection.execute(
        "SELECT amount, source FROM balances WHERE reference_date = '2026-09-27'"
    ).fetchone()
    assert (row["amount"], row["source"]) == ("3.00", "observed")

    assert db.latest_balance_date(connection, "h") == "2026-09-28"


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok  {name}")
    print("all passed")
