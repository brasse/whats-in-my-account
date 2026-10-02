"""ntfy notifications.

Off unless NTFY_TOPIC is set, so the application runs perfectly well with no
notification configuration at all.

Sending must never be able to break collection. A failed notification is logged
and swallowed: the whole point of these messages is to report trouble, and a
notifier that raises during an outage would turn a recoverable failure into a
crash at exactly the wrong moment.
"""

import datetime as dt
import logging
import sqlite3

import httpx

from wima import db
from wima.config import Settings

logger = logging.getLogger(__name__)

TIMEOUT = 10.0


async def send(settings: Settings, title: str, message: str, tags: str = "") -> bool:
    if not settings.ntfy_topic:
        # INFO rather than DEBUG: wanting to notify and being unable to is a
        # configuration mistake, and it should not be invisible at the default level.
        logger.info("no NTFY_TOPIC set, not sending: %s", title)
        return False

    # JSON rather than a Title header: headers are ASCII-only, and account labels
    # like "Lön" are not.
    body = {"topic": settings.ntfy_topic, "title": title, "message": message}
    if tags:
        body["tags"] = tags.split(",")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(settings.ntfy_url, json=body)
        if response.is_error:
            logger.warning("ntfy returned %s: %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception:
        logger.exception("could not send notification")
        return False


async def send_once(
    connection: sqlite3.Connection,
    settings: Settings,
    kind: str,
    min_interval_hours: float,
    title: str,
    message: str,
    tags: str = "",
) -> bool:
    """Send, unless this kind went out within min_interval_hours.

    The record is only written when the send actually succeeded, so a failed
    notification is retried on the next tick rather than being silently counted
    as delivered.
    """
    last = db.notification_sent_at(connection, kind)
    if last is not None:
        age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(last)
        if age < dt.timedelta(hours=min_interval_hours):
            logger.debug("%s suppressed, sent %s ago", kind, age)
            return False

    if not await send(settings, title, message, tags):
        return False

    logger.info("sent %s notification: %s", kind, title)
    db.note_notification(connection, kind)
    return True
