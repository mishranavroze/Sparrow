"""Fetch recent tweets via Nitter RSS feeds as EmailMessage objects."""

import logging
from datetime import datetime, timedelta, timezone

import feedparser

from config import settings
from src.models import EmailMessage

logger = logging.getLogger(__name__)

ACCOUNTS = [
    # AI / Tech
    "svpino", "lexfridman", "sama", "taalas_inc", "OpenAI",
    "ronitkd", "itsPaulAi", "minchoi", "swyx", "simonw",
    "championswimmer", "shreyas", "deedydas", "miramurati",
    "patrickc", "huggingface", "AiBreakfast", "The_DailyAi",
    "goodside", "_akhaliq", "karpathy", "AndrewYNg",
    "jeremyhoward", "soumithchintala", "StanfordAILab",
    "GoogleDeepMind", "AnthropicAI", "levelsio", "rowancheung",
    "TheRundownAI", "aakashgupta",
]

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]


def _fetch_feed(handle: str) -> list:
    """Try each Nitter instance until one returns a valid feed."""
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{handle}/rss"
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                continue
            if feed.entries:
                return feed.entries
        except Exception as e:
            logger.debug("Nitter instance %s failed for @%s: %s", instance, handle, e)
            continue
    return []


def fetch_todays_tweets() -> list[EmailMessage]:
    """Fetch tweets from 6:30 PM PST yesterday to 6:30 PM PST today.

    Uses the same generation schedule window as the email fetcher.

    Returns:
        List of EmailMessage objects, one per tweet.
    """
    PST = timezone(timedelta(hours=-8))
    now_pst = datetime.now(PST)
    today_pst = now_pst.date()

    # Cutoff time in PST, derived from UTC generation schedule
    cutoff_hour = (settings.generation_hour - 8) % 24  # 18 = 6:30 PM PST
    cutoff_min = settings.generation_minute

    window_end = datetime(
        today_pst.year, today_pst.month, today_pst.day,
        hour=cutoff_hour, minute=cutoff_min, tzinfo=PST,
    )
    window_start = window_end - timedelta(days=1)

    messages: list[EmailMessage] = []

    for handle in ACCOUNTS:
        try:
            entries = _fetch_feed(handle)
            for entry in entries:
                if not entry.get("published_parsed"):
                    continue
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if published < window_start or published > window_end:
                    continue

                messages.append(EmailMessage(
                    subject=entry.get("title", "")[:100],
                    sender=f"@{handle}",
                    date=published,
                    body_html="",
                    body_text=entry.get("summary", ""),
                ))
        except Exception as e:
            logger.warning("Failed to fetch tweets for @%s: %s", handle, e)
            continue

    logger.info("Fetched %d tweets from %d accounts", len(messages), len(ACCOUNTS))
    return messages
