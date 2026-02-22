"""Centralized configuration using pydantic-settings."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Gmail OAuth2
    gmail_credentials_json: str = ""
    gmail_token_json: str = ""
    gmail_label: str = "Newsletters"

    # Google account (for NotebookLM session — login is manual)
    google_account_email: str = ""
    google_account_password: str = ""

    # Gemini API for AI classification and summarization
    gemini_api_key: str = ""

    # NotebookLM
    notebooklm_notebook_url: str = ""
    chrome_user_data_dir: str = "~/.noctua-chrome-profile"

    # Podcast generation (schedule in UTC; 02:30 UTC = 6:30 PM PST)
    generation_hour: int = 2
    generation_minute: int = 30

    # Secret for external cron trigger (e.g. cron-job.org)
    cron_secret: str = ""

    # Google Cloud Storage (for permanent episode MP3 hosting)
    gcs_bucket_name: str = ""
    gcs_credentials_json: str = ""

    # Serving
    base_url: str = "http://localhost:8000"
    podcast_title: str = "The Hootline"
    podcast_description: str = (
        "Your nightly knowledge briefing. "
        "The owl of Minerva spreads its wings only with the falling of dusk."
    )

    # Multi-show support (comma-separated show IDs, e.g. "hootline,sparrow")
    show_ids: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()


@dataclass(frozen=True)
class ShowConfig:
    """Per-show configuration encapsulating all show-specific settings."""

    show_id: str
    podcast_title: str
    podcast_description: str
    gmail_credentials_json: str
    gmail_token_json: str
    gmail_label: str
    notebooklm_notebook_url: str
    google_account_email: str
    google_account_password: str
    output_dir: Path

    @property
    def db_path(self) -> Path:
        return self.output_dir / "noctua.db"

    @property
    def feed_path(self) -> Path:
        return self.output_dir / "feed.xml"

    @property
    def episodes_json_path(self) -> Path:
        return self.output_dir / "episodes.json"

    @property
    def episodes_dir(self) -> Path:
        return self.output_dir / "episodes"

    @property
    def exports_dir(self) -> Path:
        return self.output_dir / "exports"


def _get_env(key: str, default: str = "") -> str:
    """Get an env var from os.environ first, then .env file."""
    return os.environ.get(key, _dotenv_vars.get(key, default))


# Load .env file values for SHOW_* vars (pydantic-settings doesn't export
# unknown vars to os.environ, so we read the .env file directly).
_dotenv_vars = dotenv_values(".env")


def load_shows() -> dict[str, ShowConfig]:
    """Discover shows from environment variables.

    When SHOW_IDS is set (e.g. "hootline,sparrow"), reads SHOW_{ID}_*
    env vars for each show. When empty, auto-creates a single "hootline"
    show from existing flat env vars with output_dir=Path("output")
    (no subdirectory — backward compatible).
    """
    show_ids_raw = settings.show_ids.strip()

    if not show_ids_raw:
        # Legacy single-show mode: use flat env vars, output directly in output/
        return {
            "hootline": ShowConfig(
                show_id="hootline",
                podcast_title=settings.podcast_title,
                podcast_description=settings.podcast_description,
                gmail_credentials_json=settings.gmail_credentials_json,
                gmail_token_json=settings.gmail_token_json,
                gmail_label=settings.gmail_label,
                notebooklm_notebook_url=settings.notebooklm_notebook_url,
                google_account_email=settings.google_account_email,
                google_account_password=settings.google_account_password,
                output_dir=Path("output"),
            )
        }

    # Multi-show mode: read SHOW_{ID}_* env vars
    ids = [s.strip().lower() for s in show_ids_raw.split(",") if s.strip()]
    result: dict[str, ShowConfig] = {}

    for sid in ids:
        prefix = f"SHOW_{sid.upper()}_"
        result[sid] = ShowConfig(
            show_id=sid,
            podcast_title=_get_env(f"{prefix}PODCAST_TITLE", sid.title()),
            podcast_description=_get_env(f"{prefix}PODCAST_DESCRIPTION"),
            gmail_credentials_json=_get_env(f"{prefix}GMAIL_CREDENTIALS_JSON"),
            gmail_token_json=_get_env(f"{prefix}GMAIL_TOKEN_JSON"),
            gmail_label=_get_env(f"{prefix}GMAIL_LABEL", "Newsletters"),
            notebooklm_notebook_url=_get_env(f"{prefix}NOTEBOOKLM_NOTEBOOK_URL"),
            google_account_email=_get_env(f"{prefix}GOOGLE_ACCOUNT_EMAIL"),
            google_account_password=_get_env(f"{prefix}GOOGLE_ACCOUNT_PASSWORD"),
            output_dir=Path(f"output/{sid}"),
        )

    return result


shows: dict[str, ShowConfig] = load_shows()
