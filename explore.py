#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "pyjwt[crypto]"]
# ///
"""Poke at the Enable Banking API and dump raw JSON.

Nothing here is meant to survive into the real application. The point is to
find out what this particular bank actually returns before designing a schema.

Setup:
    export EB_APP_ID=<application id from the control panel>
    export EB_KEY_PATH=/path/to/private_key.pem

Usage:
    ./explore.py aspsps --country SE
    ./explore.py auth --aspsp Nordea --country SE
    ./explore.py session --code <code from the redirect URL>
    ./explore.py accounts
    ./explore.py balances <account_uid>
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import uuid

import httpx
import jwt

BASE_URL = "https://api.enablebanking.com"

# Swedbank lists these in required_psu_headers. They describe the human behind
# the request, which is honest during the browser flow and a polite fiction for
# an unattended daily fetch. Override with EB_PSU_IP / EB_PSU_UA.
PSU_IP = os.environ.get("EB_PSU_IP", "127.0.0.1")
PSU_UA = os.environ.get(
    "EB_PSU_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)

# Written by "session", read by "accounts" and "balances". Contains an access
# token to your bank account, so it stays out of git.
STATE_FILE = pathlib.Path(__file__).parent / ".explore-session.json"


def make_jwt() -> str:
    app_id = os.environ.get("EB_APP_ID")
    key_path = os.environ.get("EB_KEY_PATH")
    if not app_id or not key_path:
        sys.exit("set EB_APP_ID and EB_KEY_PATH")

    private_key = pathlib.Path(key_path).read_text()
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + 3600,
        },
        private_key,
        algorithm="RS256",
        headers={"typ": "JWT", "kid": app_id},
    )


def request(method: str, path: str, psu: bool = False, quiet: bool = False, **kwargs) -> dict:
    """One request, one dump. Errors are printed rather than raised so that a
    4xx body (which is where the useful message lives) is visible."""
    headers = {"Authorization": f"Bearer {make_jwt()}"}
    if psu:
        headers["psu-ip-address"] = PSU_IP
        headers["psu-user-agent"] = PSU_UA
    response = httpx.request(
        method,
        BASE_URL + path,
        headers=headers,
        timeout=30.0,
        **kwargs,
    )
    print(f"{method} {path} -> {response.status_code}", file=sys.stderr)
    try:
        body = response.json()
    except ValueError:
        sys.exit(f"non-JSON response: {response.text[:500]}")
    if not quiet:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    if response.is_error:
        sys.exit(1)
    return body


def load_state() -> dict:
    if not STATE_FILE.exists():
        sys.exit(f"no {STATE_FILE.name}, run the session command first")
    return json.loads(STATE_FILE.read_text())


def cmd_aspsps(args: argparse.Namespace) -> None:
    """Needs no consent, so this is also the test that the key works."""
    request("GET", "/aspsps", params={"country": args.country})


def cmd_auth(args: argparse.Namespace) -> None:
    valid_until = dt.datetime.now(dt.UTC) + dt.timedelta(days=args.days)
    body = request(
        "POST",
        "/auth",
        psu=True,
        json={
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": args.aspsp, "country": args.country},
            "state": str(uuid.uuid4()),
            "redirect_url": args.redirect_url,
            "psu_type": "personal",
        },
    )
    print(
        "\nOpen the url above, authorize, then copy the 'code' query parameter"
        "\nout of the browser address bar (the redirect page itself will not"
        "\nload, that is fine) and pass it to the session command.",
        file=sys.stderr,
    )
    return body


def cmd_session(args: argparse.Namespace) -> None:
    body = request("POST", "/sessions", psu=True, json={"code": args.code})
    STATE_FILE.write_text(json.dumps(body, indent=2))
    print(f"\nsaved to {STATE_FILE.name}", file=sys.stderr)


def cmd_accounts(args: argparse.Namespace) -> None:
    """Re-read the session to see the accounts and the expiry."""
    request("GET", f"/sessions/{load_state()['session_id']}")


def cmd_transactions(args: argparse.Namespace) -> None:
    """History, for working out whether past balances can be reconstructed.

    Swedbank answers the first call with an empty page and a continuation key,
    then serves the statement, then serves pending items. An empty page is not
    the end, only a null continuation key is.
    """
    params = {"date_from": args.date_from}
    if args.continuation_key:
        params["continuation_key"] = args.continuation_key

    if not args.follow:
        request("GET", f"/accounts/{args.uid}/transactions", psu=True, params=params)
        return

    collected = []
    for page in range(1, args.max_pages + 1):
        body = request(
            "GET",
            f"/accounts/{args.uid}/transactions",
            psu=True,
            quiet=True,
            params=params,
        )
        transactions = body.get("transactions", [])
        collected.extend(transactions)
        key = body.get("continuation_key")
        print(f"  page {page}: {len(transactions)} transactions", file=sys.stderr)
        if not key:
            break
        params["continuation_key"] = key
    else:
        print(f"stopped at --max-pages {args.max_pages}", file=sys.stderr)

    if args.dump:
        print(json.dumps(collected, indent=2, ensure_ascii=False))
        return

    dates = sorted(t["booking_date"] for t in collected if t.get("booking_date"))
    statuses = {}
    for t in collected:
        statuses[t.get("status")] = statuses.get(t.get("status"), 0) + 1

    print(
        json.dumps(
            {
                "requested_from": args.date_from,
                "total": len(collected),
                "earliest_booking_date": dates[0] if dates else None,
                "latest_booking_date": dates[-1] if dates else None,
                "by_status": statuses,
            },
            indent=2,
        )
    )


def cmd_balances(args: argparse.Namespace) -> None:
    request("GET", f"/accounts/{args.uid}/balances", psu=not args.no_psu)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("aspsps", help="list banks in a country")
    p.add_argument("--country", required=True)
    p.set_defaults(func=cmd_aspsps)

    p = sub.add_parser("auth", help="start bank authorization")
    p.add_argument("--aspsp", required=True, help="exact name from aspsps")
    p.add_argument("--country", required=True)
    p.add_argument("--days", type=int, default=180, help="requested validity")
    p.add_argument("--redirect-url", default="https://example.com/redirect")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("session", help="exchange the code for a session")
    p.add_argument("--code", required=True)
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("accounts", help="show the saved session and accounts")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("balances", help="fetch balances for one account")
    p.add_argument("uid")
    p.add_argument(
        "--no-psu",
        action="store_true",
        help="omit the psu headers, to test unattended background access",
    )
    p.set_defaults(func=cmd_balances)

    p = sub.add_parser("transactions", help="fetch transaction history")
    p.add_argument("uid")
    p.add_argument(
        "--date-from",
        default=(dt.date.today() - dt.timedelta(days=7)).isoformat(),
        help="ISO date, defaults to 7 days ago to keep the dump small",
    )
    p.add_argument("--continuation-key", help="next page, from a previous response")
    p.add_argument(
        "--follow",
        action="store_true",
        help="follow continuation keys and print a summary instead of raw JSON",
    )
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument(
        "--dump",
        action="store_true",
        help="with --follow, print the collected transactions instead of a summary",
    )
    p.set_defaults(func=cmd_transactions)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
