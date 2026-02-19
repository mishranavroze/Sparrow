"""Compile parsed articles into a single source document for NotebookLM."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone

import requests

from config import settings
from src.exceptions import DigestCompileError
from src.models import Article, CompiledDigest, DailyDigest
from src.topic_classifier import SEGMENT_DURATIONS, SEGMENT_ORDER, Topic

logger = logging.getLogger(__name__)

# NotebookLM source limit (100K characters)
MAX_SOURCE_CHARS = 100_000

# Approximate words per minute for podcast speech
WORDS_PER_MINUTE = 150

PODCAST_PREAMBLE = """\
**PODCAST PRODUCTION INSTRUCTIONS:**
This document is organized into numbered segments. When generating the podcast:
- Begin with a warm welcome
- Present each segment in order, using the segment title as a transition
- Each segment has a word budget — spend roughly proportional time on it
- Skip any segment that contains no articles
- Use a conversational but informative tone throughout
- Summarize and discuss the key points from each article, do not read them verbatim
- Transition smoothly between segments with brief bridges
- End with a brief wrap-up and sign-off
"""

INTRO_SECTION = """\
## INTRO (~1 minute)
Welcome to The Hootline, your daily knowledge briefing. Let's dive in.
"""

OUTRO_SECTION = """\
## OUTRO (~1 minute)
That's all for today's Hootline. Thanks for listening — we'll be back \
tomorrow with more. Until then, stay curious.
"""


def _build_topics_summary(digest: DailyDigest, segment_counts: dict[str, int]) -> str:
    """Build a brief topics summary reflecting active segments.

    Args:
        digest: The day's parsed articles.
        segment_counts: Mapping of segment names to article counts.

    Returns:
        Summary string listing active segments and their article counts.
    """
    parts = []
    for topic in SEGMENT_ORDER:
        count = segment_counts.get(topic.value, 0)
        if count > 0:
            parts.append(f"{topic.value} ({count})")
    return "; ".join(parts) if parts else "No segments"


def _generate_rss_summary(articles: list[Article]) -> str:
    """Generate a short (~15 word) episode description using the Claude API.

    Falls back to a generic description if the API call fails or no key is set.
    """
    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — using fallback RSS summary")
        return "Your nightly knowledge briefing from The Hootline."

    titles = [a.title for a in articles[:30]]
    titles_text = "\n".join(f"- {t}" for t in titles)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 60,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Below are today's podcast episode article titles.\n\n"
                            f"{titles_text}\n\n"
                            "Write a single sentence (~15 words) summarizing what "
                            "this episode covers. No quotes, no preamble — just the sentence."
                        ),
                    }
                ],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data["content"][0]["text"].strip().rstrip(".")
        # Ensure it's not excessively long
        if len(summary.split()) > 25:
            summary = " ".join(summary.split()[:20])
        return summary
    except Exception as e:
        logger.warning("RSS summary generation failed: %s — using fallback", e)
        return "Your nightly knowledge briefing from The Hootline."


def _parse_minutes(topic: Topic) -> int:
    """Extract the integer minutes from a SEGMENT_DURATIONS entry."""
    dur_str = SEGMENT_DURATIONS.get(topic, "~1 minute")
    return int(dur_str.replace("~", "").replace(" minutes", "").replace(" minute", ""))


def _allocate_budget(articles: list[Article], budget: int) -> dict[int, int]:
    """Allocate a character budget across articles.

    Short articles keep their full content; long articles are trimmed so
    the total fits within budget. Uses iterative redistribution: each
    round, articles that fit under the equal share lock in their full
    length, and the remaining budget is re-split among the rest.

    Args:
        articles: List of articles to allocate budget for.
        budget: Total character budget for all article content.

    Returns:
        Mapping of article index to its allowed character count.
    """
    sizes = {i: len(a.content) for i, a in enumerate(articles)}
    allocated: dict[int, int] = {}
    remaining_budget = budget
    remaining_ids = set(sizes.keys())

    while remaining_ids:
        share = remaining_budget // len(remaining_ids) if remaining_ids else 0
        settled = set()
        for i in remaining_ids:
            if sizes[i] <= share:
                allocated[i] = sizes[i]
                remaining_budget -= sizes[i]
                settled.add(i)
        remaining_ids -= settled
        # If nobody settled this round, everyone left gets the equal share
        if not settled:
            for i in remaining_ids:
                allocated[i] = share
            break

    return allocated


def _compile_text(
    digest: DailyDigest, date_str: str
) -> tuple[str, dict[str, int]]:
    """Compile articles into a segment-structured markdown document.

    Each segment gets a word budget proportional to its allocated time.
    Content is capped to fit the budget regardless of incoming volume.
    Sources are prominently listed per segment.

    Args:
        digest: The day's parsed articles.
        date_str: Formatted date string for the header.

    Returns:
        Tuple of (compiled document text, segment article counts, segment sources).
    """
    # Group articles by topic
    grouped: dict[str, list[Article]] = defaultdict(list)
    for article in digest.articles:
        topic_key = article.topic if article.topic else Topic.OTHER.value
        grouped[topic_key].append(article)

    # Calculate word budget per segment based on allocated minutes
    segment_word_budgets: dict[str, int] = {}
    for topic in SEGMENT_ORDER:
        mins = _parse_minutes(topic)
        segment_word_budgets[topic.value] = mins * WORDS_PER_MINUTE

    # Cap articles per topic — roughly 1.5 articles per minute, minimum 2
    total_before = sum(len(v) for v in grouped.values())
    for topic in SEGMENT_ORDER:
        mins = _parse_minutes(topic)
        max_articles = max(2, round(mins * 1.5))
        articles_list = grouped.get(topic.value, [])
        if len(articles_list) > max_articles:
            logger.info(
                "Capping %s from %d to %d articles (allocated %dm)",
                topic.value, len(articles_list), max_articles, mins,
            )
            grouped[topic.value] = articles_list[:max_articles]
    total_after = sum(len(v) for v in grouped.values())
    if total_after < total_before:
        logger.info("Topic capping: %d -> %d articles", total_before, total_after)

    active_topics = [t.value for t in SEGMENT_ORDER if grouped.get(t.value)]
    intro = INTRO_SECTION
    outro = OUTRO_SECTION

    # Build the document
    sections = [f"# The Hootline — Daily Briefing — {date_str}\n"]
    sections.append(PODCAST_PREAMBLE)
    sections.append(intro)

    segment_counts: dict[str, int] = {}
    segment_sources: dict[str, list[str]] = {}
    segment_number = 0

    for topic in SEGMENT_ORDER:
        articles = grouped.get(topic.value, [])
        if not articles:
            continue

        segment_number += 1
        segment_counts[topic.value] = len(articles)
        # Track unique sources for this segment
        sources = list(dict.fromkeys(a.source for a in articles))
        segment_sources[topic.value] = sources

        mins = _parse_minutes(topic)
        word_budget = segment_word_budgets[topic.value]
        duration = SEGMENT_DURATIONS.get(topic, "")
        duration_label = f" ({duration})" if duration else ""

        section_header = f"## SEGMENT {segment_number}: {topic.value}{duration_label}"
        section_header += f"\n**Word budget: ~{word_budget} words**"
        sections.append(section_header)

        # Allocate word budget across articles in this segment
        # Convert word budget to char budget (~6 chars/word + overhead)
        chars_per_word = 6  # average including spaces
        overhead_per_article = 60  # title, separator
        segment_char_budget = max(
            word_budget * chars_per_word - overhead_per_article * len(articles),
            len(articles) * 200,
        )
        article_budgets = _allocate_budget(articles, segment_char_budget)

        for i, article in enumerate(articles):
            content = article.content
            cap = article_budgets.get(i, len(content))
            if len(content) > cap:
                content = content[:cap].rsplit("\n", 1)[0] + "\n[...]"

            sections.append(f"### {article.title}")
            sections.append(content)
            sections.append("\n---\n")

    sections.append(outro)
    text = "\n\n".join(sections)

    # Final safety check against NotebookLM limit
    if len(text) > MAX_SOURCE_CHARS:
        logger.warning(
            "Compiled text %d chars exceeds %d limit, truncating",
            len(text), MAX_SOURCE_CHARS,
        )
        text = text[:MAX_SOURCE_CHARS - 100] + "\n\n[Document truncated to fit source limit.]"

    return text, segment_counts, segment_sources


def compile(digest: DailyDigest) -> CompiledDigest:
    """Compile all articles into a single well-structured text document.

    Args:
        digest: The day's parsed articles.

    Returns:
        A CompiledDigest ready for NotebookLM upload.
    """
    if not digest.articles:
        raise DigestCompileError("No articles to compile.")

    # Episode is named for tonight's PST date (the evening it airs).
    PST = timezone(timedelta(hours=-8))
    episode_date = datetime.now(PST).date()
    date_ymd = episode_date.strftime("%Y-%m-%d")
    date_display = episode_date.strftime("%B %-d, %Y")

    try:
        text, segment_counts, segment_sources = _compile_text(digest, date_display)
        topics_summary = _build_topics_summary(digest, segment_counts)
        rss_summary = _generate_rss_summary(digest.articles)
        total_words = len(text.split())

        compiled = CompiledDigest(
            text=text,
            article_count=len(digest.articles),
            total_words=total_words,
            date=date_ymd,
            topics_summary=topics_summary,
            rss_summary=rss_summary,
            segment_counts=segment_counts,
            segment_sources=segment_sources,
        )

        logger.info(
            "Compiled digest: %d articles, %d words, %d chars, %d segments",
            compiled.article_count,
            compiled.total_words,
            len(compiled.text),
            len(compiled.segment_counts),
        )

        return compiled

    except DigestCompileError:
        raise
    except Exception as e:
        raise DigestCompileError(f"Failed to compile digest: {e}") from e
