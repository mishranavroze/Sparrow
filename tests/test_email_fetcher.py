"""Tests for email_fetcher module."""

from unittest.mock import patch

import pytest

from src.content_parser import _extract_sender_name
from src.email_fetcher import _extract_body, _get_header, fetch_yesterdays_emails
from src.exceptions import EmailFetchError


def test_get_header_finds_header():
    headers = [
        {"name": "Subject", "value": "Test Subject"},
        {"name": "From", "value": "sender@example.com"},
    ]
    assert _get_header(headers, "Subject") == "Test Subject"
    assert _get_header(headers, "from") == "sender@example.com"


def test_get_header_returns_empty_for_missing():
    headers = [{"name": "Subject", "value": "Test"}]
    assert _get_header(headers, "From") == ""


def test_extract_sender_name_with_quoted():
    assert _extract_sender_name('"Morning Brew" <news@morningbrew.com>') == "Morning Brew"


def test_extract_sender_name_with_unquoted():
    assert _extract_sender_name("TLDR <tldr@example.com>") == "TLDR"


def test_extract_sender_name_email_only():
    assert _extract_sender_name("news@morningbrew.com") == "news"


def test_extract_body_plain_text():
    import base64

    text = "Hello world"
    encoded = base64.urlsafe_b64encode(text.encode()).decode()
    payload = {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }
    html, plain = _extract_body(payload)
    assert plain == "Hello world"
    assert html == ""


def test_extract_body_html():
    import base64

    html_content = "<html><body><p>Hello</p></body></html>"
    encoded = base64.urlsafe_b64encode(html_content.encode()).decode()
    payload = {
        "mimeType": "text/html",
        "body": {"data": encoded},
    }
    html, plain = _extract_body(payload)
    assert html == html_content
    assert plain == ""


def test_extract_body_multipart():
    import base64

    text = "Plain text"
    html = "<p>HTML text</p>"
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
            },
        ],
    }
    result_html, result_text = _extract_body(payload)
    assert result_text == "Plain text"
    assert result_html == "<p>HTML text</p>"


def test_fetch_yesterdays_emails_raises_without_credentials():
    with patch("src.email_fetcher.settings") as mock_settings:
        mock_settings.gmail_credentials_json = ""
        mock_settings.gmail_token_json = ""
        with pytest.raises(EmailFetchError, match="not configured"):
            fetch_yesterdays_emails()
