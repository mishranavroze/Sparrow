"""Compile parsed articles into a single source document for NotebookLM."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import requests

from config import settings
from src.exceptions import DigestCompileError
from src.models import Article, CompiledDigest, DailyDigest
from src.topic_classifier import SEGMENT_DURATIONS, SEGMENT_ORDER, Topic

logger = logging.getLogger(__name__)

# NotebookLM source limit (100K characters)
MAX_SOURCE_CHARS = 100_000

PODCAST_PREAMBLE = """\
**PODCAST PRODUCTION INSTRUCTIONS:**
This document is organized into numbered segments. When generating the podcast:
- Begin with a warm welcome and brief overview of today's topics
- Present each segment in order, using the segment title as a transition
- Spend roughly the suggested time on each segment (where noted)
- Skip any segment that contains no articles
- Use a conversational but informative tone throughout
- Transition smoothly between segments with brief bridges
- End with a brief wrap-up and sign-off
"""

INTRO_SECTION = """\
## INTRO (~1 minute)
Welcome to The Hootline, your daily knowledge briefing. Here's a quick look at \
what we're covering today: {topics_preview}. Let's dive in.
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

    Uses budget allocation to ensure the final text fits within
    MAX_SOURCE_CHARS. Short articles keep full text; long ones are
    trimmed proportionally.

    Args:
        digest: The day's parsed articles.
        date_str: Formatted date string for the header.

    Returns:
        Tuple of (compiled document text, segment article counts).
    """
    # Group articles by topic
    grouped: dict[str, list[Article]] = defaultdict(list)
    for article in digest.articles:
        topic_key = article.topic if article.topic else Topic.OTHER.value
        grouped[topic_key].append(article)

    # Cap articles per topic proportional to allocated minutes.
    # ~1.5 articles per minute, minimum 2 per topic.
    total_before = sum(len(v) for v in grouped.values())
    for topic in SEGMENT_ORDER:
        dur_str = SEGMENT_DURATIONS.get(topic, "~1 minute")
        mins = int(dur_str.replace("~", "").replace(" minutes", "").replace(" minute", ""))
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

    # Collect articles in segment order and estimate per-article overhead
    # (title line, source line, separator, joining newlines)
    ordered_articles: list[Article] = []
    for topic in SEGMENT_ORDER:
        ordered_articles.extend(grouped.get(topic.value, []))

    # Build topics preview for intro
    active_topics = [t.value for t in SEGMENT_ORDER if grouped.get(t.value)]
    topics_preview = ", ".join(active_topics[:-1]) + f", and {active_topics[-1]}" if len(active_topics) > 1 else active_topics[0] if active_topics else ""
    intro = INTRO_SECTION.format(topics_preview=topics_preview)
    outro = OUTRO_SECTION

    preamble = f"# The Hootline — Daily Briefing — {date_str}\n\n\n{PODCAST_PREAMBLE}"
    # Estimate overhead per article: "### title\n\n*Source: name*\n\n...\n\n\n---\n"
    per_article_overhead = 80  # conservative average for headers/separators
    # Segment headers overhead
    active_segments = len(active_topics)
    segment_overhead = active_segments * 60  # "## SEGMENT N: Topic (duration)\n\n"

    fixed_overhead = len(preamble) + len(intro) + len(outro) + segment_overhead + (per_article_overhead * len(ordered_articles))
    content_budget = max(MAX_SOURCE_CHARS - fixed_overhead, len(ordered_articles) * 200)

    # Allocate budget across articles
    budgets = _allocate_budget(ordered_articles, content_budget)
    trimmed_count = sum(1 for i, a in enumerate(ordered_articles) if budgets[i] < len(a.content))
    if trimmed_count:
        logger.info(
            "Budget allocation: %d/%d articles trimmed to fit %d char limit",
            trimmed_count, len(ordered_articles), MAX_SOURCE_CHARS,
        )

    # Build the document
    sections = [f"# The Hootline — Daily Briefing — {date_str}\n"]
    sections.append(PODCAST_PREAMBLE)
    sections.append(intro)

    segment_counts: dict[str, int] = {}
    segment_number = 0
    article_index = 0

    for topic in SEGMENT_ORDER:
        articles = grouped.get(topic.value, [])
        if not articles:
            continue

        segment_number += 1
        segment_counts[topic.value] = len(articles)

        duration = SEGMENT_DURATIONS.get(topic, "")
        duration_label = f" ({duration})" if duration else ""
        sections.append(
            f"## SEGMENT {segment_number}: {topic.value}{duration_label}"
        )

        for article in articles:
            content = article.content
            cap = budgets.get(article_index, len(content))
            if len(content) > cap:
                content = content[:cap].rsplit("\n", 1)[0] + "\n[...]"
            article_index += 1

            sections.append(f"### {article.title}")
            sections.append(f"*Source: {article.source}*")
            sections.append(content)
            sections.append("\n---\n")

    sections.append(outro)
    text = "\n\n".join(sections)

    return text, segment_counts


def compile(digest: DailyDigest) -> CompiledDigest:
    """Compile all articles into a single well-structured text document.

    Args:
        digest: The day's parsed articles.

    Returns:
        A CompiledDigest ready for NotebookLM upload.
    """
    if not digest.articles:
        raise DigestCompileError("No articles to compile.")

    # Generation runs at 11:30 PM PST = 07:30 UTC next day, so the UTC
    # date is already the next PST calendar day (e.g. Feb 17 PST -> Feb 18 UTC).
    episode_date = datetime.now(UTC).date()
    date_ymd = episode_date.strftime("%Y-%m-%d")
    date_display = episode_date.strftime("%B %-d, %Y")

    try:
        text, segment_counts = _compile_text(digest, date_display)
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
