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
import hmac
import os
import pathlib
import secrets
import urllib.parse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from wima import auth, collect, config, db, loop
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
    configuration = config.load_config()
    app.state.settings = settings
    app.state.config = configuration
    app.state.connection = db.connect(settings.db_path)
    app.state.accounts = configuration.accounts
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

# Only the vendored assets are public. index.html is served by a route so that
# the page itself sits behind the login like everything else.
app.mount("/static", StaticFiles(directory=STATIC / "vendor"), name="static")

# Reachable without logging in. /health exists so the container healthcheck does
# not need credentials, and it reveals nothing.
PUBLIC_PATHS = {"/login", "/health"}


def safe_next(path: str) -> str:
    """Only same-site absolute paths, so ?next= cannot become an open redirect."""
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


async def form_values(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body.

    Starlette's request.form() requires python-multipart even for urlencoded
    bodies, which a single login form does not justify.
    """
    parsed = urllib.parse.parse_qs(
        (await request.body()).decode("utf-8"), keep_blank_values=True
    )
    return {key: values[0] for key, values in parsed.items()}


def login_page(next_path: str, error: str = "") -> HTMLResponse:
    html = (STATIC / "login.html").read_text()
    html = html.replace("__NEXT__", urllib.parse.quote(next_path, safe="/?=&"))
    html = html.replace("__ERROR__", error)
    html = html.replace("__ERROR_HIDDEN__", "" if error else "hidden")
    return HTMLResponse(html, status_code=401 if error else 200)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE_NAME)
    if token and auth.session_is_valid(request.app.state.connection, token):
        return await call_next(request)

    # The page's fetch calls need a status they can act on, not a login form
    # rendered into a JSON parser.
    if path.startswith("/api/"):
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    target = path
    if request.url.query:
        target = f"{path}?{request.url.query}"
    return RedirectResponse(f"/login?next={urllib.parse.quote(target, safe='')}", 303)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/login")
async def login_form(next: str = "/") -> HTMLResponse:
    return login_page(safe_next(next))


@app.post("/login")
async def login(request: Request):
    form = await form_values(request)
    username = form.get("username", "")
    password = form.get("password", "")
    next_path = safe_next(form.get("next", "/"))

    expected = request.app.state.config.auth
    # Both checks always run: comparing the username first and returning early
    # would let someone learn a valid username from the response time.
    name_ok = hmac.compare_digest(username, expected.username)
    password_ok = auth.verify_password(password, expected.password_hash)
    if not (name_ok and password_ok):
        logger.warning("failed login for %r", username[:40])
        return login_page(next_path, error="Wrong username or password")

    token = auth.start_session(request.app.state.connection)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        # Caddy terminates TLS, so the app itself only ever sees http. Whether
        # the cookie may travel in the clear is decided by the public URL.
        secure=bool(
            request.app.state.settings.public_url
            and request.app.state.settings.public_url.startswith("https://")
        ),
    )
    logger.info("logged in")
    return response


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.end_session(request.app.state.connection, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


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
        # Eight characters is plenty to find the commit, and fits in a footer.
        "commit": commit[:8] if (commit := app.state.settings.commit) else None,
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


class HideHealthChecks(logging.Filter):
    """Drop access log lines for /health, which the container healthcheck polls
    every 30 seconds and would otherwise drown out everything else."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access args: client, method, path, http version, status.
        args = record.args
        return not (isinstance(args, tuple) and len(args) > 2 and args[2] == "/health")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Fail on bad configuration here rather than inside the lifespan, where the
    # same sys.exit surfaces as a startup traceback. A missing file should print
    # one line, not a stack.
    config.load_settings()
    config.load_config()

    logging.getLogger("uvicorn.access").addFilter(HideHealthChecks())

    uvicorn.run(
        app,
        host=os.environ.get("WIMA_HOST", "127.0.0.1"),
        port=int(os.environ.get("WIMA_PORT", "8081")),
        log_level="info",
        # uvicorn's own config formats its lines without a timestamp. Without it,
        # they propagate to the root logger and share the format above.
        log_config=None,
    )


if __name__ == "__main__":
    main()
