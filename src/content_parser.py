"""HTML email content parsing — extract clean text from newsletters."""

import logging
import re
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

from src.exceptions import ContentParseError
from src.models import Article, DailyDigest, EmailMessage
from src.topic_classifier import classify_article, _is_filtered_sender

logger = logging.getLogger(__name__)

# Elements to strip from newsletter HTML
STRIP_TAGS = [
    "script", "style", "nav", "footer", "header",
    "iframe", "noscript", "svg", "form",
]

# Patterns indicating junk content to remove
JUNK_PATTERNS = re.compile(
    r"(unsubscribe|manage\s+preferences|view\s+in\s+browser|email\s+preferences|"
    r"update\s+your\s+profile|powered\s+by|©\s*\d{4}|all\s+rights\s+reserved|"
    r"privacy\s+policy|terms\s+of\s+service|follow\s+us\s+on)",
    re.IGNORECASE,
)

# Tracking pixel patterns
TRACKING_PIXEL_PATTERN = re.compile(
    r'<img[^>]+(width=["\']1["\']|height=["\']1["\']|'
    r"tracking|pixel|beacon|open\.gif|t\.gif)[^>]*>",
    re.IGNORECASE,
)

SIMILARITY_THRESHOLD = 0.6


def _clean_html(html: str) -> str:
    """Strip junk elements from HTML and return clean text.

    Args:
        html: Raw HTML content.

    Returns:
        Cleaned plain text.
    """
    # Remove tracking pixels before parsing
    html = TRACKING_PIXEL_PATTERN.sub("", html)

    soup = BeautifulSoup(html, "lxml")

    # Remove junk tags entirely
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove hidden elements
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none")):
        tag.decompose()

    # Remove images that are tracking pixels (1x1)
    for img in soup.find_all("img"):
        width = img.get("width", "")
        height = img.get("height", "")
        if width in ("1", "0") or height in ("1", "0"):
            img.decompose()

    # Convert links to plain text (drop URLs to save space for NotebookLM)
    for a_tag in soup.find_all("a"):
        text = a_tag.get_text(strip=True)
        if text:
            a_tag.replace_with(text)

    # Get text content
    text = soup.get_text(separator="\n")

    # Clean up whitespace
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and not JUNK_PATTERNS.search(line):
            lines.append(line)

    return "\n".join(lines)


def _extract_sender_name(sender: str) -> str:
    """Extract a clean sender name from email From header.

    Args:
        sender: Raw From header value like '"Morning Brew" <email@example.com>'.

    Returns:
        Clean sender name.
    """
    # Remove email address portion
    match = re.match(r'^"?([^"<]+)"?\s*<', sender)
    if match:
        return match.group(1).strip()
    return sender.split("@")[0] if "@" in sender else sender


def _is_similar(text_a: str, text_b: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """Check if two texts are similar enough to be considered duplicates.

    Uses first 500 chars for efficiency.
    """
    sample_a = text_a[:500].lower()
    sample_b = text_b[:500].lower()
    return SequenceMatcher(None, sample_a, sample_b).ratio() > threshold


def _deduplicate_articles(articles: list[Article]) -> list[Article]:
    """Remove duplicate articles based on content similarity."""
    if len(articles) <= 1:
        return articles

    unique: list[Article] = []
    for article in articles:
        is_dup = False
        for existing in unique:
            if _is_similar(article.content, existing.content):
                logger.info(
                    "Deduplicating: '%s' similar to '%s'", article.title, existing.title
                )
                is_dup = True
                break
        if not is_dup:
            unique.append(article)

    return unique


def parse_emails(emails: list[EmailMessage]) -> DailyDigest:
    """Parse a list of email messages into a daily digest.

    Args:
        emails: List of raw email messages to parse.

    Returns:
        A DailyDigest containing extracted articles.
    """
    articles: list[Article] = []

    for email in emails:
        try:
            # Early filter: skip transactional senders before HTML parsing
            source = _extract_sender_name(email.sender)
            if _is_filtered_sender(source):
                logger.info("Skipping transactional sender: '%s'", source)
                continue

            content = ""
            if email.body_html:
                content = _clean_html(email.body_html)
            elif email.body_text:
                content = email.body_text.strip()

            if not content or len(content) < 50:
                logger.warning("Skipping email '%s' — too little content", email.subject)
                continue

            word_count = len(content.split())

            article = Article(
                source=source,
                title=email.subject,
                content=content,
                estimated_words=word_count,
            )

            # Classify article into a topic segment
            topic = classify_article(article)
            if topic is None:
                logger.info("Filtered article by classification: '%s'", email.subject)
                continue
            article.topic = topic.value

            articles.append(article)

        except Exception as e:
            logger.error("Failed to parse email '%s': %s", email.subject, e)
            raise ContentParseError(
                f"Failed to parse email '{email.subject}': {e}"
            ) from e

    articles = _deduplicate_articles(articles)
    total_words = sum(a.estimated_words for a in articles)

    logger.info("Parsed %d articles (%d words)", len(articles), total_words)

    return DailyDigest(articles=articles, total_words=total_words)
