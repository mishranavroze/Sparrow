"""Data models for the Noctua pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class EmailMessage:
    """A fetched email from Gmail."""

    subject: str
    sender: str
    date: datetime
    body_html: str
    body_text: str = ""


@dataclass
class Article:
    """A parsed article extracted from a newsletter email."""

    source: str
    title: str
    content: str
    estimated_words: int


@dataclass
class DailyDigest:
    """Collection of articles for a single day."""

    articles: list[Article]
    total_words: int
    date: datetime = field(default_factory=datetime.now)


@dataclass
class CompiledDigest:
    """A compiled source document ready for NotebookLM upload."""

    text: str
    article_count: int
    total_words: int
    date: str  # YYYY-MM-DD
    topics_summary: str


@dataclass
class EpisodeMetadata:
    """Metadata for a generated podcast episode."""

    date: str  # YYYY-MM-DD
    file_path: Path
    file_size_bytes: int
    duration_seconds: int
    duration_formatted: str  # HH:MM:SS
    topics_summary: str
