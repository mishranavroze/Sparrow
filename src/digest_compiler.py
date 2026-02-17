"""Compile parsed articles into a single source document for NotebookLM."""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from src.exceptions import DigestCompileError
from src.models import Article, CompiledDigest, DailyDigest
from src.topic_classifier import SEGMENT_DURATIONS, SEGMENT_ORDER, Topic

logger = logging.getLogger(__name__)

# NotebookLM source limit (~500K characters)
MAX_SOURCE_CHARS = 500_000

PODCAST_PREAMBLE = """\
**PODCAST PRODUCTION INSTRUCTIONS:**
This document is organized into numbered segments. When generating the podcast:
- Present each segment in order, using the segment title as a transition
- Spend roughly the suggested time on each segment (where noted)
- Skip any segment that contains no articles
- Use a conversational but informative tone throughout
- Transition smoothly between segments with brief bridges
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


def _compile_text(
    digest: DailyDigest, date_str: str
) -> tuple[str, dict[str, int]]:
    """Compile articles into a segment-structured markdown document.

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

    sections = [f"# Noctua Daily Briefing — {date_str}\n"]
    sections.append(PODCAST_PREAMBLE)

    segment_counts: dict[str, int] = {}
    segment_number = 0

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
            sections.append(f"### {article.title}")
            sections.append(f"*Source: {article.source}*")
            sections.append(article.content)
            sections.append("\n---\n")

    text = "\n\n".join(sections)

    # Truncate if exceeding NotebookLM's source limit
    if len(text) > MAX_SOURCE_CHARS:
        logger.warning(
            "Digest exceeds %d chars (%d). Truncating.",
            MAX_SOURCE_CHARS,
            len(text),
        )
        text = text[:MAX_SOURCE_CHARS]
        # Find last complete section boundary
        last_separator = text.rfind("\n---\n")
        if last_separator > 0:
            text = text[: last_separator + 5]

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

    yesterday = datetime.now(UTC) - timedelta(days=1)
    date_ymd = yesterday.strftime("%Y-%m-%d")
    date_display = yesterday.strftime("%B %d, %Y")

    try:
        text, segment_counts = _compile_text(digest, date_display)
        topics_summary = _build_topics_summary(digest, segment_counts)
        total_words = len(text.split())

        compiled = CompiledDigest(
            text=text,
            article_count=len(digest.articles),
            total_words=total_words,
            date=date_ymd,
            topics_summary=topics_summary,
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
