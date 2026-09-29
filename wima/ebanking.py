"""Enable Banking API client.

Every request carries a short-lived JWT signed with the application's private
key. The PSU headers describe the human behind the request, which Swedbank
lists in required_psu_headers.
"""

import datetime as dt

import httpx
import jwt

from wima.config import Settings

BASE_URL = "https://api.enablebanking.com"
TIMEOUT = 30.0


class SessionNotAuthorized(Exception):
    """The session is no longer usable and only a browser re-authorization fixes it.

    Worth its own type: an expired consent needs you, a 500 from Swedbank needs
    nothing but patience, and the notification should say which one happened.
    """

    def __init__(self, status: str, valid_until: str | None) -> None:
        super().__init__(f"session status is {status}, valid_until {valid_until}")
        self.status = status
        self.valid_until = valid_until


class EnableBankingError(Exception):
    """A non-2xx response. Carries the body, which is where the useful part is."""

    def __init__(self, method: str, path: str, status: int, body: object) -> None:
        super().__init__(f"{method} {path} -> {status}: {body}")
        self.status = status
        self.body = body


class EnableBanking:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)

    def _make_jwt(self) -> str:
        now = int(dt.datetime.now(dt.UTC).timestamp())
        return jwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": now + 3600,
            },
            self._settings.private_key,
            algorithm="RS256",
            headers={"typ": "JWT", "kid": self._settings.app_id},
        )

    async def _request(self, method: str, path: str, psu: bool = True, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self._make_jwt()}"}
        if psu:
            headers["psu-ip-address"] = self._settings.psu_ip
            headers["psu-user-agent"] = self._settings.psu_user_agent

        response = await self._client.request(method, path, headers=headers, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        if response.is_error:
            raise EnableBankingError(method, path, response.status_code, body)
        return body

    async def start_auth(
        self, aspsp_name: str, country: str, redirect_url: str, state: str, days: int
    ) -> dict:
        valid_until = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
        return await self._request(
            "POST",
            "/auth",
            json={
                "access": {"valid_until": valid_until.isoformat()},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": "personal",
            },
        )

    async def create_session(self, code: str) -> dict:
        return await self._request("POST", "/sessions", json={"code": code})

    async def get_session(self, session_id: str) -> dict:
        return await self._request("GET", f"/sessions/{session_id}")

    async def uid_by_hash(self, session_id: str) -> dict[str, str]:
        """Map stable account hashes to the uids this session addresses them by.

        Note 'accounts_data', not 'accounts'. The latter is a bare list of uid
        strings, which carries no way to tell which account is which.
        """
        session = await self.get_session(session_id)
        if session.get("status") != "AUTHORIZED":
            raise SessionNotAuthorized(
                session.get("status"), session.get("access", {}).get("valid_until")
            )
        return {
            entry["identification_hash"]: entry["uid"]
            for entry in session["accounts_data"]
        }

    async def get_balances(self, uid: str) -> list[dict]:
        body = await self._request("GET", f"/accounts/{uid}/balances")
        return body["balances"]

    async def get_transactions(
        self, uid: str, date_from: str, continuation_key: str | None = None
    ) -> dict:
        """One page. Swedbank answers the first call with an empty page and a
        continuation key, so callers must follow keys until one comes back null
        rather than stopping at the first empty page."""
        params = {"date_from": date_from}
        if continuation_key:
            params["continuation_key"] = continuation_key
        return await self._request("GET", f"/accounts/{uid}/transactions", params=params)
