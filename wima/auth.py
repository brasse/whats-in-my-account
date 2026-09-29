"""Password hashing and login sessions.

Passwords are hashed with scrypt from the standard library, which is memory hard
and needs no dependency.

Login sessions are deliberately boring: the cookie is an opaque random string,
and the server stores only its SHA-256. Nothing is signed or encrypted here, so
there is no home-made crypto to get wrong. The server either recognises a token
or it does not. Storing the hash rather than the token means a copy of the
database does not hand someone a working session.
"""

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3

from wima import db

# Cost parameters. n * r * 128 bytes of memory, so 32 MiB at these values.
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
SCRYPT_DKLEN = 32

SESSION_DAYS = 30
COOKIE_NAME = "wima_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """scrypt$n$r$p$salt$key, all base64, parameters inline so they can change."""
    if salt is None:
        salt = secrets.token_bytes(16)

    key = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(key).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Recompute with the stored parameters and compare in constant time."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(actual, expected)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(connection: sqlite3.Connection) -> str:
    """Create a session and return the token to put in the cookie.

    The token is returned once and never stored, only its digest is.
    """
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=SESSION_DAYS)
    db.create_web_session(connection, token_digest(token), expires.isoformat())
    return token


def session_is_valid(connection: sqlite3.Connection, token: str) -> bool:
    return db.web_session_is_valid(connection, token_digest(token))


def end_session(connection: sqlite3.Connection, token: str) -> None:
    db.delete_web_session(connection, token_digest(token))
