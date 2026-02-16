"""Tests for digest_compiler module."""

from datetime import UTC, datetime

import pytest

from src.digest_compiler import MAX_SOURCE_CHARS, _build_topics_summary, _compile_text, compile
from src.exceptions import DigestCompileError
from src.models import Article, DailyDigest


def _make_digest(num_articles: int = 3) -> DailyDigest:
    """Create a test digest with the given number of articles."""
    articles = [
        Article(
            source=f"Source {i}",
            title=f"Article Title {i}",
            content=f"This is the content of article {i}. " * 20,
            estimated_words=100,
        )
        for i in range(num_articles)
    ]
    return DailyDigest(
        articles=articles,
        total_words=sum(a.estimated_words for a in articles),
        date=datetime.now(UTC),
    )


def test_compile_basic():
    digest = _make_digest(2)
    result = compile(digest)
    assert result.article_count == 2
    assert result.total_words > 0
    assert "Article Title 0" in result.text
    assert "Article Title 1" in result.text
    assert "Source 0" in result.text
    assert result.date  # YYYY-MM-DD format
    assert len(result.topics_summary) > 0


def test_compile_empty_raises():
    digest = DailyDigest(articles=[], total_words=0)
    with pytest.raises(DigestCompileError, match="No articles"):
        compile(digest)


def test_compile_text_includes_structure():
    digest = _make_digest(1)
    text = _compile_text(digest, "February 16, 2026")
    assert "# Daily News Digest" in text
    assert "## From: Source 0" in text
    assert "### Article Title 0" in text
    assert "---" in text


def test_compile_text_truncates_long_content():
    # Create a digest that exceeds the limit
    articles = [
        Article(
            source="Source",
            title="Big Article",
            content="x" * (MAX_SOURCE_CHARS + 1000),
            estimated_words=100_000,
        )
    ]
    digest = DailyDigest(articles=articles, total_words=100_000)
    text = _compile_text(digest, "Feb 16, 2026")
    assert len(text) <= MAX_SOURCE_CHARS + 100  # small margin for header


def test_build_topics_summary():
    digest = _make_digest(3)
    summary = _build_topics_summary(digest)
    assert "Article Title 0" in summary
    assert "Article Title 1" in summary
    assert "Article Title 2" in summary
    assert ";" in summary


def test_compile_single_article():
    digest = _make_digest(1)
    result = compile(digest)
    assert result.article_count == 1
    assert "Article Title 0" in result.text
