"""Centralized configuration using pydantic-settings."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

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
    podcast_title: str = ""
    podcast_description: str = ""

    # Optional override: comma-separated show IDs to activate (e.g. "sparrow,nighthawk").
    # When empty, all shows defined in shows.json are activated.
    show_ids: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

# Timezone: America/Los_Angeles handles both PST (UTC-8) and PDT (UTC-7) automatically.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# shows.json loader — single source of truth for show definitions
# ---------------------------------------------------------------------------

_SHOWS_JSON_PATH = Path("shows.json")


def _load_shows_json() -> dict:
    """Load show definitions from shows.json."""
    if not _SHOWS_JSON_PATH.exists():
        raise FileNotFoundError(
            f"shows.json not found at {_SHOWS_JSON_PATH.resolve()}. "
            "This file is required for show configuration."
        )
    return json.loads(_SHOWS_JSON_PATH.read_text())


_shows_json = _load_shows_json()

DEFAULT_SHOW_ID: str = _shows_json.get("default_show", "")
APP_TITLE: str = _shows_json.get("app_title", "Noctua Podcast Platform")


# ---------------------------------------------------------------------------
# ShowFormat — built dynamically from shows.json
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShowFormat:
    """Per-show segment format defining which topics and how long each gets."""

    segments: tuple[tuple[str, int], ...]  # (topic_name, minutes) pairs
    intro_minutes: float = 1.0
    outro_minutes: float = 1.0

    @property
    def segment_order(self) -> list[str]:
        return [name for name, _ in self.segments]

    @property
    def segment_durations(self) -> dict[str, int]:
        return {name: mins for name, mins in self.segments}

    @property
    def total_minutes(self) -> int:
        return int(self.intro_minutes + sum(m for _, m in self.segments) + self.outro_minutes)


SHOW_FORMATS: dict[str, ShowFormat] = {}
for _sid, _sdef in _shows_json.get("shows", {}).items():
    SHOW_FORMATS[_sid] = ShowFormat(
        segments=tuple(tuple(s) for s in _sdef["segments"]),
        intro_minutes=_sdef.get("intro_minutes", 1.0),
        outro_minutes=_sdef.get("outro_minutes", 1.0),
    )

_DEFAULT_FORMAT = SHOW_FORMATS.get(
    DEFAULT_SHOW_ID, next(iter(SHOW_FORMATS.values()), ShowFormat(segments=()))
)


# ---------------------------------------------------------------------------
# ShowConfig
# ---------------------------------------------------------------------------

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
    icon_filename: str = "noctua_owl.png"
    weather_location: str = ""

    @property
    def format(self) -> ShowFormat:
        return SHOW_FORMATS.get(self.show_id, _DEFAULT_FORMAT)

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


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _get_env(key: str, default: str = "") -> str:
    """Get an env var from os.environ first, then .env file."""
    return os.environ.get(key, _dotenv_vars.get(key, default))


# Load .env file values for SHOW_* vars (pydantic-settings doesn't export
# unknown vars to os.environ, so we read the .env file directly).
_dotenv_vars = dotenv_values(".env")


# ---------------------------------------------------------------------------
# Show loading — reads metadata from shows.json, secrets from env vars
# ---------------------------------------------------------------------------

def load_shows() -> dict[str, ShowConfig]:
    """Discover shows from shows.json and load secrets from env vars.

    Show metadata (title, description, segments, icon, weather) comes from
    shows.json. Secrets (Gmail creds, NotebookLM URL, Google account) come
    from SHOW_{ID}_* environment variables.

    The optional SHOW_IDS env var can filter which shows to activate.
    When empty, all shows defined in shows.json are activated.
    """
    show_defs = _shows_json.get("shows", {})

    # Optional env var override for which shows to activate
    env_show_ids = settings.show_ids.strip()
    if env_show_ids:
        active_ids = [s.strip().lower() for s in env_show_ids.split(",") if s.strip()]
    else:
        active_ids = list(show_defs.keys())

    result: dict[str, ShowConfig] = {}

    for sid in active_ids:
        sdef = show_defs.get(sid)
        if not sdef:
            raise ValueError(
                f"Show '{sid}' listed in SHOW_IDS but not defined in shows.json"
            )
        prefix = f"SHOW_{sid.upper()}_"
        result[sid] = ShowConfig(
            show_id=sid,
            podcast_title=sdef.get("podcast_title", sid.title()),
            podcast_description=sdef.get("podcast_description", ""),
            gmail_credentials_json=_get_env(f"{prefix}GMAIL_CREDENTIALS_JSON"),
            gmail_token_json=_get_env(f"{prefix}GMAIL_TOKEN_JSON"),
            gmail_label=_get_env(f"{prefix}GMAIL_LABEL", "Newsletters"),
            notebooklm_notebook_url=_get_env(f"{prefix}NOTEBOOKLM_NOTEBOOK_URL"),
            google_account_email=_get_env(f"{prefix}GOOGLE_ACCOUNT_EMAIL"),
            google_account_password=_get_env(f"{prefix}GOOGLE_ACCOUNT_PASSWORD"),
            output_dir=Path(f"output/{sid}"),
            icon_filename=sdef.get("icon_filename", "noctua_owl.png"),
            weather_location=sdef.get("weather_location", ""),
        )

    return result


shows: dict[str, ShowConfig] = load_shows()
