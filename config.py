"""Centralized configuration using pydantic-settings."""

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

    # Optional: Claude API for digest summarization
    anthropic_api_key: str = ""

    # NotebookLM
    notebooklm_notebook_url: str = ""
    chrome_user_data_dir: str = "~/.noctua-chrome-profile"

    # Podcast generation
    generation_hour: int = 18

    # Serving
    base_url: str = "http://localhost:8000"
    podcast_title: str = "The Hootline"
    podcast_description: str = (
        "Your nightly knowledge briefing. "
        "The owl of Minerva spreads its wings only with the falling of dusk."
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
