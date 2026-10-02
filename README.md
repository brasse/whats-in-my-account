# What's in my account

A small self-hosted tool that records your bank balance once a day and draws a
graph of it. It reads balances through the [Enable Banking](https://enablebanking.com)
PSD2 API, stores them in SQLite, and serves a single page with a chart and a few
controls for the time period.

Built for a home network, behind a reverse proxy, for one person. Access is
behind a login form with a single user whose password hash lives in the config
file.

## How it works

One process does both jobs. A background task wakes every hour and asks whether
today has been collected yet, collecting if not and sleeping if so. It does not
start before 06:00, because just after midnight the bank still reports
yesterday's balance. There is no
cron, so a machine that was off at 04:00 catches up as soon as it is running
again rather than losing the day.

Two things are worth knowing because they shape everything else:

**Authorization expires.** PSD2 access is granted for a limited period, up to
180 days at Swedbank, after which you have to approve it again in a browser.
There is a `/auth` endpoint for that, the page shows a warning as the date
approaches, and ntfy notifications can remind you.

**History only reaches back 90 days.** On first run the service reconstructs
past balances by walking backwards through booked transactions from today's
balance, which gives roughly 90 days of history immediately. The same mechanism
heals any gap left by a lapsed authorization. Beyond 90 days the bank will not
serve the transactions, so a longer lapse loses that data permanently.

Reconstructed values are stored as `derived` and drawn as a dashed line.
Values read directly from the bank are `observed`, and a derived value can never
overwrite one.

## Daily balance notifications

Mark an account with `notify = true` in `config.toml` and its available balance
is sent to `NTFY_TOPIC` after each day's first successful collection, along with
the change since the previous reading:

```
Privatkonto 1234
12 345.67 SEK (−432.10 since yesterday)
```

On a fresh install the change appears from the second day on. Backfill can only
reconstruct the booked balance, so there is no earlier available balance to
compare against until one has actually been read. A failed send is logged and
not retried; tomorrow's message supersedes it.

## Running it

With Docker, which is the intended way:

```bash
mkdir -p data
cp config.toml.example data/config.toml     # then fill it in
cp /path/to/downloaded-key.pem data/private-key.pem
cp compose.example.yaml compose.yaml        # then fill in the environment
docker compose up -d
```

Everything that is not in the image lives in `data/`: the config file, the
private key and the database. That directory is the only thing to back up, and
restoring it restores the service completely.

Without Docker, using [uv](https://docs.astral.sh/uv/):

```bash
uv run python -m wima.web
```

## First-time setup

1. Create an application in the Enable Banking control panel, note the
   application ID and download the private key.
2. Register `https://your.host/auth/callback` as a redirect URL there. Matching
   is an exact string comparison, so the path has to be included.
3. Start the service and visit `/auth` to authorize with your bank.
4. Find your accounts' `identification_hash` values and list the ones you want
   in `config.toml`. `explore.py` is a scratch script for poking at the API by
   hand, and can dump a session response for you.
5. Generate the login credentials with `python -m wima.passwd` and paste the
   `[auth]` block it prints into `config.toml`. The service will not start
   without it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `EB_APP_ID` | required | Application ID from the control panel |
| `EB_KEY_PATH` | `/data/private-key.pem` | RSA private key for signing requests |
| `WIMA_PUBLIC_URL` | unset | External base URL, needed for the authorization flow |
| `WIMA_DB` | `/data/balances.db` | SQLite database |
| `WIMA_CONFIG` | `/data/config.toml` | Login and which accounts to collect |
| `WIMA_HOST` / `WIMA_PORT` | `0.0.0.0` / `8081` | Where to listen |
| `WIMA_ASPSP` / `WIMA_COUNTRY` | `Swedbank` / `SE` | Which bank |
| `WIMA_CONSENT_DAYS` | `180` | Access duration to request |
| `NTFY_TOPIC` | unset | Where alerts and daily balances go, skipped when unset |
| `NTFY_URL` | `https://ntfy.sh` | For a self-hosted ntfy |
| `TZ` | container default | Decides where one day ends |

## Tests

```bash
for t in tests/test_*.py; do uv run python "$t"; done
```

Plain asserts, no framework. They cover the reconstruction arithmetic, the rule
that derived never overwrites observed, when notifications fire and when they
stay quiet, password hashing and sessions, and the login wall and the guards on
the authorization flow.

## Bank support

Written against Swedbank, but nothing here is Swedbank-specific beyond the
defaults. Other banks in the Enable Banking catalogue should work by changing
`WIMA_ASPSP` and `WIMA_COUNTRY`, though the shape of what they return varies:
some populate `balance_after_transaction`, which would make the reconstruction a
lookup rather than arithmetic, and the available balance types differ.
