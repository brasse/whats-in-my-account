"""The web application, and the process that hosts the collector.

One process, one event loop. The collector runs as a background task alongside
the HTTP server rather than as a second container or a cron entry, so there is a
single thing to deploy and a single place to look when something is wrong.

The SQLite connection is shared and every endpoint is async, which keeps all
database access on the event loop thread. Queries here are local, indexed and
tiny, so the blocking is measured in microseconds. A def endpoint would be run
in a threadpool and SQLite would refuse the cross-thread access.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import os
import pathlib
import secrets

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from wima import collect, config, db, loop
from wima.ebanking import EnableBanking

logger = logging.getLogger(__name__)

STATIC = pathlib.Path(__file__).parent / "static"

# How long after the last success the page starts complaining.
STALE_AFTER_HOURS = loop.STALE_AFTER_HOURS


async def supervise(
    connection, client: EnableBanking, accounts: list[config.Account], settings: config.Settings
) -> None:
    """Keep the collector running even if run_forever somehow returns or raises.

    run_forever already swallows everything it can. This exists for the case it
    cannot handle, which is a bug in its own error handling, and it makes that
    case loud and recoverable instead of silent and permanent.
    """
    while True:
        try:
            await loop.run_forever(connection, client, accounts, settings)
            logger.error("collector returned unexpectedly, restarting in 60s")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("collector died, restarting in 60s")
        await asyncio.sleep(60)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config.load_settings()
    app.state.settings = settings
    app.state.connection = db.connect(settings.db_path)
    app.state.accounts = config.load_accounts()
    app.state.client = EnableBanking(settings)
    # state token -> when it was issued, so a callback can be matched to a flow
    # this process actually started.
    app.state.pending_auth = {}

    task = asyncio.create_task(
        supervise(
            app.state.connection, app.state.client, app.state.accounts, settings
        )
    )
    yield
    task.cancel()


app = FastAPI(title="whats-in-my-account", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def account_by_id(app: FastAPI, account_id: str) -> config.Account:
    for account in app.state.accounts:
        if account.id == account_id:
            return account
    raise HTTPException(status_code=404, detail="no such account")


@app.get("/api/accounts")
async def api_accounts() -> list[dict]:
    return [
        {"id": account.id, "label": account.label} for account in app.state.accounts
    ]


@app.get("/api/balances")
async def api_balances(
    balance_type: str = Query("ITBD", alias="type"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> dict:
    """Every configured account's series, for one balance type.

    Amounts become floats here, at the boundary, purely because this is feeding a
    chart. The database keeps the bank's exact strings.
    """
    series = []
    for account in app.state.accounts:
        rows = db.balance_series(
            app.state.connection, account.hash, balance_type, date_from, date_to
        )
        series.append(
            {
                "id": account.id,
                "label": account.label,
                "currency": rows[0]["currency"] if rows else None,
                "points": [
                    {
                        "date": row["reference_date"],
                        "amount": float(row["amount"]),
                        "source": row["source"],
                    }
                    for row in rows
                ],
            }
        )
    return {"type": balance_type, "accounts": series}


@app.get("/api/status")
async def api_status() -> dict:
    """Everything the page needs to decide whether to shout at you."""
    connection = app.state.connection
    now = dt.datetime.now(dt.UTC)

    last_success = db.last_successful_run(connection)
    hours_since_success = None
    if last_success is not None:
        delta = now - dt.datetime.fromisoformat(last_success["started_at"])
        hours_since_success = round(delta.total_seconds() / 3600, 1)

    last_run = connection.execute(
        "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    session = db.get_session(connection)
    consent_days_left = None
    if session is not None:
        remaining = dt.datetime.fromisoformat(session["valid_until"]) - now
        consent_days_left = remaining.days

    return {
        "last_success": last_success["started_at"] if last_success else None,
        "hours_since_success": hours_since_success,
        "stale": hours_since_success is None or hours_since_success > STALE_AFTER_HOURS,
        "last_error": last_run["error"] if last_run and not last_run["ok"] else None,
        "consent_days_left": consent_days_left,
        "consent_valid_until": session["valid_until"] if session else None,
        # Without a public URL the bank has nowhere to redirect back to, so the
        # page must not offer a button that cannot work.
        "can_reauthorize": app.state.settings.public_url is not None,
    }


AUTH_FLOW_TIMEOUT = dt.timedelta(minutes=15)


@app.get("/auth")
async def start_auth() -> RedirectResponse:
    """Begin a browser re-authorization and send you to the bank."""
    settings = app.state.settings
    if not settings.public_url:
        raise HTTPException(
            status_code=500,
            detail="WIMA_PUBLIC_URL is not set, so the bank has nowhere to send you back to",
        )

    now = dt.datetime.now(dt.UTC)
    pending = app.state.pending_auth
    for token, issued in list(pending.items()):
        if now - issued > AUTH_FLOW_TIMEOUT:
            del pending[token]

    state = secrets.token_urlsafe(16)
    pending[state] = now

    body = await app.state.client.start_auth(
        aspsp_name=settings.aspsp_name,
        country=settings.aspsp_country,
        redirect_url=f"{settings.public_url}/auth/callback",
        state=state,
        days=settings.consent_days,
    )
    logger.info("starting authorization with %s", settings.aspsp_name)
    return RedirectResponse(body["url"])


@app.get("/auth/callback")
async def auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish the flow the bank just sent the browser back from.

    Collection runs inline rather than waiting for the next hourly tick, so any
    gap is healed while you are still looking at the page that caused it.
    """
    if error:
        raise HTTPException(status_code=400, detail=f"the bank returned an error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    if app.state.pending_auth.pop(state, None) is None:
        raise HTTPException(
            status_code=400,
            detail="unknown or expired state, start again from /auth",
        )

    session = await app.state.client.create_session(code)
    db.set_session(
        app.state.connection, session["session_id"], session["access"]["valid_until"]
    )
    logger.info("authorized, valid until %s", session["access"]["valid_until"])

    try:
        written = await collect.run(
            app.state.connection, app.state.client, app.state.accounts
        )
        logger.info("collected %d rows after authorization", written)
    except Exception:
        # The authorization succeeded, which is the part that needed you. A failed
        # first collection is the hourly loop's problem, not a reason to show an error.
        logger.exception("collection after authorization failed")

    return RedirectResponse("/", status_code=303)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Fail on bad configuration here rather than inside the lifespan, where the
    # same sys.exit surfaces as a startup traceback. A missing file should print
    # one line, not a stack.
    config.load_settings()
    config.load_accounts()

    uvicorn.run(
        app,
        host=os.environ.get("WIMA_HOST", "127.0.0.1"),
        port=int(os.environ.get("WIMA_PORT", "8081")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
