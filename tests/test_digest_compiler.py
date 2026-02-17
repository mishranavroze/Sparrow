"""Tests for digest_compiler module."""

from datetime import UTC, datetime

import pytest

from src.digest_compiler import (
    MAX_SOURCE_CHARS,
    PODCAST_PREAMBLE,
    _build_topics_summary,
    _compile_text,
    compile,
)
from src.exceptions import DigestCompileError
from src.models import Article, DailyDigest
from src.topic_classifier import Topic


def _make_article(
    source: str = "Source",
    title: str = "Title",
    content: str = "This is the content of the article. " * 20,
    topic: str = Topic.OTHER.value,
) -> Article:
    return Article(
        source=source,
        title=title,
        content=content,
        estimated_words=len(content.split()),
        topic=topic,
    )


def _make_digest(articles: list[Article] | None = None) -> DailyDigest:
    """Create a test digest."""
    if articles is None:
        articles = [
            _make_article(
                source="NYT", title="Senate Vote", topic=Topic.US_POLITICS.value
            ),
            _make_article(
                source="The Neuron", title="AI Update", topic=Topic.TECH_AI.value
            ),
            _make_article(
                source="1440", title="NATO Summit", topic=Topic.WORLD_POLITICS.value
            ),
        ]
    return DailyDigest(
        articles=articles,
        total_words=sum(a.estimated_words for a in articles),
        date=datetime.now(UTC),
    )


def test_compile_basic():
    digest = _make_digest()
    result = compile(digest)
    assert result.article_count == 3
    assert result.total_words > 0
    assert "Senate Vote" in result.text
    assert "AI Update" in result.text
    assert "NATO Summit" in result.text
    assert result.date  # YYYY-MM-DD format
    assert len(result.topics_summary) > 0


def test_compile_empty_raises():
    digest = DailyDigest(articles=[], total_words=0)
    with pytest.raises(DigestCompileError, match="No articles"):
        compile(digest)


def test_compile_text_includes_segment_structure():
    digest = _make_digest()
    text, segment_counts = _compile_text(digest, "February 17, 2026")
    assert "# Noctua Daily Briefing" in text
    assert "SEGMENT" in text
    assert "---" in text


def test_compile_text_includes_preamble():
    digest = _make_digest()
    text, _ = _compile_text(digest, "February 17, 2026")
    assert "PODCAST PRODUCTION INSTRUCTIONS" in text


def test_compile_text_segments_in_order():
    """Segments should appear in the defined order: World Politics before US Politics before Tech."""
    articles = [
        _make_article(source="Neuron", title="Tech Article", topic=Topic.TECH_AI.value),
        _make_article(source="NYT", title="US Politics Article", topic=Topic.US_POLITICS.value),
        _make_article(source="1440", title="World Politics Article", topic=Topic.WORLD_POLITICS.value),
    ]
    digest = _make_digest(articles)
    text, _ = _compile_text(digest, "February 17, 2026")

    world_pos = text.index("World Politics Article")
    us_pos = text.index("US Politics Article")
    tech_pos = text.index("Tech Article")

    assert world_pos < us_pos < tech_pos


def test_compile_text_skips_empty_segments():
    """Only segments with articles should appear."""
    articles = [
        _make_article(source="NYT", title="Only Tech", topic=Topic.TECH_AI.value),
    ]
    digest = _make_digest(articles)
    text, segment_counts = _compile_text(digest, "February 17, 2026")

    assert "Latest in Tech" in text
    assert "World Politics" not in text
    assert "US Politics" not in text
    assert "CrossFit" not in text
    assert segment_counts == {Topic.TECH_AI.value: 1}


def test_compile_text_segment_counts():
    articles = [
        _make_article(source="NYT", title="Article 1", topic=Topic.US_POLITICS.value),
        _make_article(source="1440", title="Article 2", topic=Topic.US_POLITICS.value),
        _make_article(source="Neuron", title="Article 3", topic=Topic.TECH_AI.value),
    ]
    digest = _make_digest(articles)
    _, segment_counts = _compile_text(digest, "February 17, 2026")

    assert segment_counts[Topic.US_POLITICS.value] == 2
    assert segment_counts[Topic.TECH_AI.value] == 1


def test_compile_text_shows_source():
    """Articles should show *Source: name* format."""
    articles = [
        _make_article(source="The New York Times", title="Test", topic=Topic.OTHER.value),
    ]
    digest = _make_digest(articles)
    text, _ = _compile_text(digest, "February 17, 2026")

    assert "*Source: The New York Times*" in text


def test_compile_text_truncates_long_content():
    articles = [
        Article(
            source="Source",
            title="Big Article",
            content="x" * (MAX_SOURCE_CHARS + 1000),
            estimated_words=100_000,
            topic=Topic.OTHER.value,
        )
    ]
    digest = DailyDigest(articles=articles, total_words=100_000)
    text, _ = _compile_text(digest, "Feb 17, 2026")
    assert len(text) <= MAX_SOURCE_CHARS + 500  # margin for headers + preamble


def test_build_topics_summary():
    segment_counts = {
        Topic.US_POLITICS.value: 2,
        Topic.TECH_AI.value: 1,
        Topic.WORLD_POLITICS.value: 3,
    }
    digest = _make_digest()
    summary = _build_topics_summary(digest, segment_counts)

    # Should list segments in order
    assert "World Politics (3)" in summary
    assert "US Politics (2)" in summary
    assert "Latest in Tech (1)" in summary


def test_build_topics_summary_empty():
    digest = _make_digest()
    summary = _build_topics_summary(digest, {})
    assert summary == "No segments"


def test_compile_single_article():
    articles = [_make_article(topic=Topic.OTHER.value)]
    digest = _make_digest(articles)
    result = compile(digest)
    assert result.article_count == 1
    assert "Title" in result.text


def test_compile_returns_segment_counts():
    digest = _make_digest()
    result = compile(digest)
    assert isinstance(result.segment_counts, dict)
    assert len(result.segment_counts) > 0


def test_compile_text_duration_labels():
    """World Politics and US Politics should have ~5 minutes labels."""
    articles = [
        _make_article(source="NYT", title="World News", topic=Topic.WORLD_POLITICS.value),
        _make_article(source="NYT", title="US News", topic=Topic.US_POLITICS.value),
    ]
    digest = _make_digest(articles)
    text, _ = _compile_text(digest, "February 17, 2026")

    assert "~5 minutes" in text
