"""Configuration, from the environment and from accounts.toml."""

import dataclasses
import hashlib
import os
import pathlib
import sys
import tomllib
from typing import Any


@dataclasses.dataclass(frozen=True)
class Account:
    hash: str
    label: str
    # Send this account's available balance to NTFY_TOPIC after each day's
    # collection.
    notify: bool = False

    @property
    def id(self) -> str:
        """Short, stable, URL-safe handle derived from the hash.

        The identification_hash itself is long, full of base64 punctuation, and
        identifies a real bank account, none of which belongs in a query string.
        """
        return hashlib.sha256(self.hash.encode()).hexdigest()[:8]


@dataclasses.dataclass(frozen=True)
class Auth:
    username: str
    password_hash: str


@dataclasses.dataclass(frozen=True)
class Config:
    """The contents of config.toml: who may log in, and what to collect."""

    auth: Auth
    accounts: list[Account]


@dataclasses.dataclass(frozen=True)
class Settings:
    app_id: str
    private_key: str
    psu_ip: str
    psu_user_agent: str
    db_path: pathlib.Path
    # Notifications are off unless a topic is set, so nothing has to be
    # configured to run this thing.
    ntfy_url: str
    ntfy_topic: str | None
    aspsp_name: str
    aspsp_country: str
    consent_days: int
    # The externally reachable base URL, as Caddy serves it. Needed to build the
    # redirect the bank sends the browser back to, and to put a working link in
    # a notification. Without it the browser flow cannot run.
    public_url: str | None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def load_settings() -> Settings:
    app_id = os.environ.get("EB_APP_ID")
    key_path = os.environ.get("EB_KEY_PATH")
    if not app_id or not key_path:
        sys.exit("set EB_APP_ID and EB_KEY_PATH")

    key = pathlib.Path(key_path)
    if not key.is_file():
        sys.exit(f"no private key at {key}, set EB_KEY_PATH or put it there")

    return Settings(
        app_id=app_id,
        private_key=key.read_text(),
        psu_ip=os.environ.get("EB_PSU_IP", "127.0.0.1"),
        psu_user_agent=os.environ.get("EB_PSU_UA", DEFAULT_USER_AGENT),
        db_path=pathlib.Path(os.environ.get("WIMA_DB", "balances.db")),
        ntfy_url=os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC"),
        aspsp_name=os.environ.get("WIMA_ASPSP", "Swedbank"),
        aspsp_country=os.environ.get("WIMA_COUNTRY", "SE"),
        consent_days=int(os.environ.get("WIMA_CONSENT_DAYS", "180")),
        public_url=_public_url(),
    )


def _public_url() -> str | None:
    url = os.environ.get("WIMA_PUBLIC_URL")
    return url.rstrip("/") if url else None


def config_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("WIMA_CONFIG", "config.toml"))


def load_config(path: pathlib.Path | None = None) -> Config:
    """Read config.toml. WIMA_CONFIG lets a container mount it anywhere."""
    if path is None:
        path = config_path()

    if not path.exists():
        sys.exit(f"{path} not found, copy config.toml.example and fill it in")

    with path.open("rb") as f:
        data = tomllib.load(f)

    accounts = [_account(entry, path) for entry in data.get("account", [])]
    if not accounts:
        sys.exit(f"{path} lists no accounts")

    # Mandatory rather than optional: an unprotected deployment should not be one
    # forgotten section away, and this page shows your bank balance.
    auth = data.get("auth", {})
    if not auth.get("username") or not auth.get("password_hash"):
        sys.exit(
            f"{path} has no [auth] section with a username and password_hash.\n"
            f"Run: python -m wima.passwd"
        )

    return Config(
        auth=Auth(username=auth["username"], password_hash=auth["password_hash"]),
        accounts=accounts,
    )


def _account(entry: dict[str, Any], path: pathlib.Path) -> Account:
    # A quoted "false" is a non-empty string, so it would opt the account in.
    notify = entry.get("notify", False)
    if not isinstance(notify, bool):
        sys.exit(
            f"{path}: notify for account {entry['label']!r} must be a boolean.\n"
            "Write notify = true or notify = false, without quotes."
        )
    return Account(hash=entry["hash"], label=entry["label"], notify=notify)


def load_accounts(path: pathlib.Path | None = None) -> list[Account]:
    return load_config(path).accounts
