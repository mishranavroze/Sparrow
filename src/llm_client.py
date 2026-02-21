"""Thin wrapper around the Google Gemini API."""

import logging
import time

import requests

from config import settings
from src.exceptions import ClaudeAPIError

logger = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

FLASH_MODEL = "gemini-2.0-flash"
PRO_MODEL = "gemini-2.0-flash"  # Flash for both — fast, cheap, high quality

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]  # seconds to wait between retries


def _call_gemini(
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: int = 30,
) -> str:
    """Make a Gemini API call with retry on 429/5xx errors.

    Args:
        model: Model ID to use.
        system: System instruction.
        user_message: User message content.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.
        timeout: Request timeout in seconds.

    Returns:
        The generated text.

    Raises:
        ClaudeAPIError: If the API key is missing or all retries fail.
    """
    if not settings.gemini_api_key:
        raise ClaudeAPIError("No GEMINI_API_KEY configured")

    url = f"{API_URL}/{model}:generateContent?key={settings.gemini_api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user_message}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                headers={"content-type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Gemini API %d (attempt %d/%d), retrying in %ds...",
                    resp.status_code, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                last_error = f"{resp.status_code}: {resp.text[:200]}"
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.exceptions.HTTPError as e:
            raise ClaudeAPIError(f"Gemini API call failed: {e}") from e
        except ClaudeAPIError:
            raise
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Gemini API error (attempt %d/%d): %s, retrying in %ds...",
                    attempt + 1, MAX_RETRIES, e, wait,
                )
                time.sleep(wait)
            continue

    raise ClaudeAPIError(f"Gemini API failed after {MAX_RETRIES} retries: {last_error}")


def call_haiku(
    system: str,
    user_message: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: int = 30,
) -> str:
    """Call Gemini Flash (fast/cheap, good for classification)."""
    return _call_gemini(FLASH_MODEL, system, user_message, max_tokens, temperature, timeout)


def call_sonnet(
    system: str,
    user_message: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    timeout: int = 60,
) -> str:
    """Call Gemini Flash (good for summarization)."""
    return _call_gemini(PRO_MODEL, system, user_message, max_tokens, temperature, timeout)
