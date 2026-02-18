"""Upload episode MP3 files to Google Cloud Storage."""

import json
import logging
from pathlib import Path

from google.cloud import storage
from google.oauth2 import service_account

from config import settings

logger = logging.getLogger(__name__)


def _get_client() -> storage.Client:
    """Create a GCS client from service account credentials."""
    creds_info = json.loads(settings.gcs_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(creds_info)
    return storage.Client(credentials=credentials, project=credentials.project_id)


def upload_episode(local_path: Path, date: str) -> str:
    """Upload an episode MP3 to GCS and return the public URL.

    Args:
        local_path: Path to the local MP3 file.
        date: Episode date string (YYYY-MM-DD).

    Returns:
        Public URL of the uploaded file.
    """
    bucket_name = settings.gcs_bucket_name
    blob_name = f"episodes/noctua-{date}.mp3"

    client = _get_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    blob.upload_from_filename(str(local_path), content_type="audio/mpeg")
    blob.make_public()

    url = f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
    logger.info("Uploaded episode to GCS: %s", url)
    return url


def is_configured() -> bool:
    """Check if GCS storage is configured."""
    return bool(settings.gcs_bucket_name and settings.gcs_credentials_json)
