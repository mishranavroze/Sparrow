"""Tests for content_parser module."""

from datetime import UTC, datetime

from src.content_parser import (
    _clean_html,
    _deduplicate_articles,
    _extract_sender_name,
    _is_similar,
    parse_emails,
)
from src.models import Article, EmailMessage


def test_clean_html_strips_scripts_and_styles():
    html = "<html><script>alert('x')</script><style>.x{}</style><p>Content</p></html>"
    result = _clean_html(html)
    assert "alert" not in result
    assert ".x" not in result
    assert "Content" in result


def test_clean_html_strips_tracking_pixels():
    html = (
        '<html><body><img width="1" height="1" src="track.gif">'
        "<p>Real content here for testing</p></body></html>"
    )
    result = _clean_html(html)
    assert "track.gif" not in result
    assert "Real content" in result


def test_clean_html_strips_unsubscribe():
    html = (
        "<html><body><p>Good article content here</p>"
        "<p>Click to unsubscribe from this list</p></body></html>"
    )
    result = _clean_html(html)
    assert "Good article" in result
    assert "unsubscribe" not in result.lower()


def test_clean_html_converts_links():
    html = (
        '<html><body><p>Read <a href="https://example.com">'
        "this article</a></p></body></html>"
    )
    result = _clean_html(html)
    assert "this article" in result
    assert "https://example.com" in result


def test_clean_html_strips_hidden_elements():
    html = (
        '<html><body><div style="display: none;">hidden</div>'
        "<p>visible</p></body></html>"
    )
    result = _clean_html(html)
    assert "hidden" not in result
    assert "visible" in result


def test_extract_sender_name():
    assert _extract_sender_name('"Morning Brew" <news@brew.com>') == "Morning Brew"
    assert _extract_sender_name("TLDR <hi@tldr.tech>") == "TLDR"


def test_is_similar_detects_duplicates():
    text_a = "The Federal Reserve announced today that interest rates remain unchanged."
    text_b = "The Federal Reserve announced today that interest rates remain unchanged."
    assert _is_similar(text_a, text_b) is True


def test_is_similar_rejects_different():
    text_a = "Apple launches new iPhone model with advanced features."
    text_b = "NASA discovers new exoplanet in habitable zone of nearby star."
    assert _is_similar(text_a, text_b) is False


def test_deduplicate_articles():
    content_a = "Unique content about topic A " * 20
    content_b = "Different content about topic B " * 20
    articles = [
        Article(source="Source A", title="Title 1", content=content_a, estimated_words=80),
        Article(source="Source B", title="Title 2", content=content_a, estimated_words=80),
        Article(source="Source C", title="Title 3", content=content_b, estimated_words=80),
    ]
    result = _deduplicate_articles(articles)
    assert len(result) == 2


def test_parse_emails_basic():
    body = (
        "<html><body><p>This is a substantial newsletter article "
        "with enough content to pass the minimum threshold for "
        "processing.</p></body></html>"
    )
    emails = [
        EmailMessage(
            subject="Daily Newsletter",
            sender='"Test News" <news@test.com>',
            date=datetime.now(UTC),
            body_html=body,
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 1
    assert digest.articles[0].source == "Test News"
    assert digest.articles[0].title == "Daily Newsletter"
    assert digest.total_words > 0


def test_parse_emails_skips_short_content():
    emails = [
        EmailMessage(
            subject="Short",
            sender="test@test.com",
            date=datetime.now(UTC),
            body_html="<html><body><p>Hi</p></body></html>",
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 0


def test_parse_emails_empty_list():
    digest = parse_emails([])
    assert len(digest.articles) == 0
    assert digest.total_words == 0


def test_parse_emails_falls_back_to_text():
    plain = (
        "This is a substantial plain text newsletter with enough "
        "content to pass the minimum threshold for processing and testing."
    )
    emails = [
        EmailMessage(
            subject="Plain Text Newsletter",
            sender="newsletter@example.com",
            date=datetime.now(UTC),
            body_html="",
            body_text=plain,
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 1
    assert "plain text newsletter" in digest.articles[0].content.lower()


# --- Transactional filtering ---


def test_parse_emails_filters_transactional_sender():
    body = (
        "<html><body><p>Your Google storage is almost full. "
        "Upgrade to Google One for more space and benefits.</p></body></html>"
    )
    emails = [
        EmailMessage(
            subject="Storage almost full",
            sender='"Google" <no-reply@google.com>',
            date=datetime.now(UTC),
            body_html=body,
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 0


def test_parse_emails_filters_notebooklm():
    body = (
        "<html><body><p>Your notebook has been updated with new sources "
        "and is ready for audio generation processing.</p></body></html>"
    )
    emails = [
        EmailMessage(
            subject="Notebook updated",
            sender='"NotebookLM" <notebooklm@google.com>',
            date=datetime.now(UTC),
            body_html=body,
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 0


# --- Topic assignment ---


def test_parse_emails_assigns_topic():
    body = (
        "<html><body><p>Congress passed a bipartisan bill in the Senate "
        "today as Democrats and Republicans reached a deal on legislation.</p></body></html>"
    )
    emails = [
        EmailMessage(
            subject="Senate Passes Major Bill",
            sender='"The New York Times" <nyt@nytimes.com>',
            date=datetime.now(UTC),
            body_html=body,
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 1
    assert digest.articles[0].topic != ""


def test_parse_emails_assigns_topic_from_source():
    body = (
        "<html><body><p>This week in AI: new developments in machine learning "
        "and the latest startup funding rounds in the tech industry.</p></body></html>"
    )
    emails = [
        EmailMessage(
            subject="AI Weekly",
            sender='"The Neuron" <hello@theneuron.com>',
            date=datetime.now(UTC),
            body_html=body,
            body_text="",
        ),
    ]
    digest = parse_emails(emails)
    assert len(digest.articles) == 1
    assert digest.articles[0].topic == "Latest in Tech"
