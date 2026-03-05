"""Gmail API integration — fetch newsletter emails for the daily digest."""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config import LOCAL_TZ, ShowConfig, settings
from src.exceptions import EmailFetchError
from src.models import EmailMessage

logger = logging.getLogger(__name__)


def _get_gmail_service(show: ShowConfig | None = None):
    """Build and return an authenticated Gmail API service."""
    creds_json = show.gmail_credentials_json if show else settings.gmail_credentials_json
    token_json = show.gmail_token_json if show else settings.gmail_token_json

    if not creds_json or not token_json:
        raise EmailFetchError("Gmail credentials or token not configured.")

    try:
        token_data = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(token_data)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        raise EmailFetchError(f"Failed to authenticate with Gmail: {e}") from e


def _extract_body(payload: dict) -> tuple[str, str]:
    """Extract HTML and plain text body from a Gmail message payload.

    Args:
        payload: The message payload from Gmail API.

    Returns:
        Tuple of (body_html, body_text).
    """
    body_html = ""
    body_text = ""

    def _walk_parts(parts):
        nonlocal body_html, body_text
        for part in parts:
            mime_type = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")

            if mime_type == "text/html" and data:
                body_html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            elif mime_type == "text/plain" and data:
                body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

            if "parts" in part:
                _walk_parts(part["parts"])

    mime_type = payload.get("mimeType", "")
    if mime_type.startswith("multipart/"):
        _walk_parts(payload.get("parts", []))
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if mime_type == "text/html":
                body_html = decoded
            else:
                body_text = decoded

    return body_html, body_text


def _get_header(headers: list[dict], name: str) -> str:
    """Get a header value by name from Gmail message headers."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def fetch_todays_emails(show: ShowConfig | None = None) -> list[EmailMessage]:
    """Fetch newsletter emails for today's episode (24-hour window).

    Uses the configured generation schedule to compute a rolling 24-hour
    window: from yesterday's cutoff time to today's cutoff time (PST).
    Epoch timestamps ensure precise boundaries with no overlap between
    consecutive digests.

    Args:
        show: Show-specific config for Gmail credentials and label.

    Returns:
        List of EmailMessage objects for today's newsletters.
    """
    service = _get_gmail_service(show)
    gmail_label = show.gmail_label if show else settings.gmail_label

    now_local = datetime.now(LOCAL_TZ)
    today_local = now_local.date()

    # Derive local cutoff time from the UTC generation schedule
    cutoff_utc = datetime(
        today_local.year, today_local.month, today_local.day,
        hour=settings.generation_hour, minute=settings.generation_minute,
        tzinfo=timezone.utc,
    )
    cutoff_local = cutoff_utc.astimezone(LOCAL_TZ)
    cutoff_hour = cutoff_local.hour
    cutoff_min = cutoff_local.minute

    # Today's cutoff
    cutoff_today = datetime(
        today_local.year, today_local.month, today_local.day,
        hour=cutoff_hour, minute=cutoff_min, tzinfo=LOCAL_TZ,
    )

    # 24-hour window: previous cutoff → now
    start_boundary = cutoff_today - timedelta(days=1)
    after_epoch = int(start_boundary.timestamp())
    before_epoch = int(now_local.timestamp())
    query = f"after:{after_epoch} before:{before_epoch}"
    if gmail_label:
        query = f"label:{gmail_label} {query}"

    logger.info("Querying Gmail: %s", query)

    messages: list[EmailMessage] = []
    page_token = None

    try:
        while True:
            result = (
                service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token)
                .execute()
            )

            message_refs = result.get("messages", [])
            if not message_refs:
                break

            for ref in message_refs:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=ref["id"], format="full")
                    .execute()
                )

                payload = msg.get("payload", {})
                headers = payload.get("headers", [])

                subject = _get_header(headers, "Subject")
                sender = _get_header(headers, "From")
                date_str = _get_header(headers, "Date")

                try:
                    date = datetime.strptime(date_str[:31], "%a, %d %b %Y %H:%M:%S %z")
                except (ValueError, IndexError):
                    date = datetime.now(UTC)

                body_html, body_text = _extract_body(payload)

                messages.append(
                    EmailMessage(
                        subject=subject,
                        sender=sender,
                        date=date,
                        body_html=body_html,
                        body_text=body_text,
                    )
                )

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        logger.info("Fetched %d emails", len(messages))
        return messages

    except EmailFetchError:
        raise
    except Exception as e:
        raise EmailFetchError(f"Failed to fetch emails: {e}") from e
