"""Configuration, from the environment and from accounts.toml."""

import dataclasses
import hashlib
import os
import pathlib
import sys
import tomllib


@dataclasses.dataclass(frozen=True)
class Account:
    hash: str
    label: str

    @property
    def id(self) -> str:
        """Short, stable, URL-safe handle derived from the hash.

        The identification_hash itself is long, full of base64 punctuation, and
        identifies a real bank account, none of which belongs in a query string.
        """
        return hashlib.sha256(self.hash.encode()).hexdigest()[:8]


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

    return Settings(
        app_id=app_id,
        private_key=pathlib.Path(key_path).read_text(),
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


def load_accounts(path: pathlib.Path | None = None) -> list[Account]:
    """Accounts to collect. WIMA_ACCOUNTS lets a container mount this anywhere."""
    if path is None:
        path = pathlib.Path(os.environ.get("WIMA_ACCOUNTS", "accounts.toml"))

    if not path.exists():
        sys.exit(f"{path} not found, copy accounts.toml.example and fill it in")

    with path.open("rb") as f:
        data = tomllib.load(f)

    accounts = [
        Account(hash=entry["hash"], label=entry["label"])
        for entry in data.get("account", [])
    ]
    if not accounts:
        sys.exit(f"{path} lists no accounts")
    return accounts
