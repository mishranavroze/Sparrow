"""Compile parsed articles into a single source document for NotebookLM."""

import logging
from datetime import UTC, datetime

from src.exceptions import DigestCompileError
from src.models import CompiledDigest, DailyDigest

logger = logging.getLogger(__name__)

# NotebookLM source limit (~500K characters)
MAX_SOURCE_CHARS = 500_000


def _build_topics_summary(digest: DailyDigest) -> str:
    """Build a brief topics summary from article titles for RSS description.

    Args:
        digest: The day's parsed articles.

    Returns:
        Comma-separated list of topics.
    """
    titles = [a.title for a in digest.articles[:10]]
    return "; ".join(titles)


def _compile_text(digest: DailyDigest, date_str: str) -> str:
    """Compile articles into a single well-structured markdown document.

    Args:
        digest: The day's parsed articles.
        date_str: Formatted date string for the header.

    Returns:
        The compiled document text.
    """
    sections = [f"# Daily News Digest — {date_str}\n"]

    for article in digest.articles:
        sections.append(f"## From: {article.source}")
        sections.append(f"### {article.title}")
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

    return text


def compile(digest: DailyDigest) -> CompiledDigest:
    """Compile all articles into a single well-structured text document.

    Args:
        digest: The day's parsed articles.

    Returns:
        A CompiledDigest ready for NotebookLM upload.
    """
    if not digest.articles:
        raise DigestCompileError("No articles to compile.")

    today = datetime.now(UTC)
    date_ymd = today.strftime("%Y-%m-%d")
    date_display = today.strftime("%B %d, %Y")

    try:
        text = _compile_text(digest, date_display)
        topics_summary = _build_topics_summary(digest)
        total_words = len(text.split())

        compiled = CompiledDigest(
            text=text,
            article_count=len(digest.articles),
            total_words=total_words,
            date=date_ymd,
            topics_summary=topics_summary,
        )

        logger.info(
            "Compiled digest: %d articles, %d words, %d chars",
            compiled.article_count,
            compiled.total_words,
            len(compiled.text),
        )

        return compiled

    except DigestCompileError:
        raise
    except Exception as e:
        raise DigestCompileError(f"Failed to compile digest: {e}") from e
