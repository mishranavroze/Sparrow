"""Backfill digests for missed dates.

Usage:
    python scripts/backfill.py 2026-02-22
    python scripts/backfill.py 2026-02-22 2026-02-23
"""

import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import shows
from src import content_parser, database, digest_compiler, email_fetcher
from src.models import EmailMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backfill")

PST = timezone(timedelta(hours=-8))


def fetch_emails_for_range(start_pst: datetime, end_pst: datetime, show=None) -> list[EmailMessage]:
    """Fetch emails within a specific PST time range."""
    service = email_fetcher._get_gmail_service(show)
    gmail_label = show.gmail_label if show else email_fetcher.settings.gmail_label

    after_epoch = int(start_pst.timestamp())
    before_epoch = int(end_pst.timestamp())
    query = f"after:{after_epoch} before:{before_epoch}"
    if gmail_label:
        query = f"label:{gmail_label} {query}"

    logger.info("Querying Gmail: %s", query)
    logger.info("  Range: %s → %s PST", start_pst.strftime("%Y-%m-%d %H:%M"), end_pst.strftime("%Y-%m-%d %H:%M"))

    messages: list[EmailMessage] = []
    page_token = None

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

            subject = email_fetcher._get_header(headers, "Subject")
            sender = email_fetcher._get_header(headers, "From")
            date_str = email_fetcher._get_header(headers, "Date")

            try:
                date = datetime.strptime(date_str[:31], "%a, %d %b %Y %H:%M:%S %z")
            except (ValueError, IndexError):
                from datetime import UTC
                date = datetime.now(UTC)

            body_html, body_text = email_fetcher._extract_body(payload)
            messages.append(
                EmailMessage(
                    subject=subject, sender=sender, date=date,
                    body_html=body_html, body_text=body_text,
                )
            )

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    logger.info("Fetched %d emails", len(messages))
    return messages


def backfill_date(target_date_str: str, show=None):
    """Generate a digest for a specific date.

    The email window is 6:30 PM PST the day before → 6:30 PM PST on the target date.
    """
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()

    # 6:30 PM PST cutoff (matches GENERATION_HOUR=2, GENERATION_MINUTE=30 UTC → 18:30 PST)
    cutoff_hour, cutoff_min = 18, 30

    end_pst = datetime(target_date.year, target_date.month, target_date.day,
                       cutoff_hour, cutoff_min, tzinfo=PST)
    start_pst = end_pst - timedelta(days=1)

    logger.info("=== Backfilling digest for %s ===", target_date_str)

    # 1. Fetch emails
    emails = fetch_emails_for_range(start_pst, end_pst, show=show)
    if not emails:
        logger.warning("No emails found for %s. Skipping.", target_date_str)
        return

    # 2. Parse
    digest = content_parser.parse_emails(emails)
    if not digest.articles:
        logger.warning("No articles extracted for %s. Skipping.", target_date_str)
        return
    logger.info("Parsed %d articles, %d words", len(digest.articles), digest.total_words)

    # 3. Compile with the correct date (patch datetime.now to return the target date)
    target_dt = datetime(target_date.year, target_date.month, target_date.day,
                         cutoff_hour, cutoff_min, tzinfo=PST)

    def fake_now(tz=None):
        if tz is not None:
            return target_dt.astimezone(tz)
        return target_dt

    with patch("src.digest_compiler.datetime") as mock_dt:
        mock_dt.now = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        compiled = digest_compiler.compile(digest, show=show)

    compiled.email_count = len(emails)

    # Force the correct date
    compiled.date = target_date_str

    # 4. Save to DB
    db_path = show.db_path if show else None
    database.save_digest(
        date=compiled.date,
        markdown_text=compiled.text,
        article_count=compiled.article_count,
        total_words=compiled.total_words,
        topics_summary=compiled.topics_summary,
        rss_summary=compiled.rss_summary,
        email_count=compiled.email_count,
        segment_counts=compiled.segment_counts,
        segment_sources=compiled.segment_sources,
        db_path=db_path,
    )

    logger.info("Saved digest for %s: %d articles, %d words",
                target_date_str, compiled.article_count, compiled.total_words)
    return compiled


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/backfill.py YYYY-MM-DD [YYYY-MM-DD ...]")
        sys.exit(1)

    dates = sys.argv[1:]
    show = next(iter(shows.values())) if shows else None

    for date_str in dates:
        try:
            backfill_date(date_str, show=show)
        except Exception as e:
            logger.error("Backfill failed for %s: %s", date_str, e)


if __name__ == "__main__":
    main()
