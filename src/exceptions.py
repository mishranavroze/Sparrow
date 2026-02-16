"""Custom exception hierarchy for Noctua."""


class NoctuaError(Exception):
    """Base exception for all Noctua errors."""


class EmailFetchError(NoctuaError):
    """Raised when fetching emails from Gmail fails."""


class ContentParseError(NoctuaError):
    """Raised when parsing email content fails."""


class DigestCompileError(NoctuaError):
    """Raised when compiling the daily digest fails."""


class NotebookLMError(NoctuaError):
    """Raised when NotebookLM automation fails."""


class SelectorNotFoundError(NotebookLMError):
    """Raised when a UI element cannot be found with any selector strategy."""


class AudioGenerationTimeoutError(NotebookLMError):
    """Raised when audio generation exceeds the timeout."""


class SessionExpiredError(NotebookLMError):
    """Raised when the Google session has expired and manual re-login is needed."""


class EpisodeProcessError(NoctuaError):
    """Raised when processing a downloaded episode fails."""


class FeedBuildError(NoctuaError):
    """Raised when building the RSS feed fails."""
