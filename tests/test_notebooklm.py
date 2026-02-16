"""Tests for notebooklm module."""

import pytest

from src.exceptions import (
    AudioGenerationTimeoutError,
    NotebookLMError,
    SelectorNotFoundError,
    SessionExpiredError,
)


def test_exception_hierarchy():
    """Verify NotebookLM exception hierarchy."""
    assert issubclass(NotebookLMError, Exception)
    assert issubclass(SelectorNotFoundError, NotebookLMError)
    assert issubclass(AudioGenerationTimeoutError, NotebookLMError)
    assert issubclass(SessionExpiredError, NotebookLMError)


def test_selector_not_found_error():
    with pytest.raises(SelectorNotFoundError):
        raise SelectorNotFoundError("test element", ["#sel1", "#sel2"])


def test_session_expired_error():
    with pytest.raises(SessionExpiredError):
        raise SessionExpiredError("session expired")


def test_audio_generation_timeout_error():
    with pytest.raises(AudioGenerationTimeoutError):
        raise AudioGenerationTimeoutError("timed out after 900s")


def test_automator_importable():
    """Verify the NotebookLMAutomator class can be imported (when Playwright is available)."""
    try:
        from src.notebooklm import NotebookLMAutomator

        automator = NotebookLMAutomator()
        assert automator is not None
        assert callable(automator.generate_episode)
    except ImportError:
        pytest.skip("Playwright not available in this environment")
