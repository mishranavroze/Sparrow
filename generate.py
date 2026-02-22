"""Orchestrator — daily podcast generation pipeline."""

import asyncio
import logging
import sys
import uuid

from config import ShowConfig
from src import (
    content_parser,
    database,
    digest_compiler,
    email_fetcher,
)
from src.exceptions import (
    ContentParseError,
    DigestCompileError,
    EmailFetchError,
    NoctuaError,
)
from src.models import CompiledDigest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def generate_digest_only(show: ShowConfig | None = None) -> CompiledDigest | None:
    """Run steps 1-3 of the pipeline: fetch emails, parse, compile digest.

    Args:
        show: Show-specific config. When None, uses legacy defaults.

    Returns:
        The compiled digest, or None if no emails/articles were found.
    """
    db_path = show.db_path if show else None
    run_id = uuid.uuid4().hex[:12]
    database.start_run(run_id, db_path=db_path)

    try:
        # 1. Fetch emails
        logger.info("Step 1/3: Fetching today's emails...")
        database.log_step(run_id, "1. Fetch emails", "running", db_path=db_path)
        try:
            emails = email_fetcher.fetch_todays_emails(show=show)

            if not emails:
                logger.info("No emails found. Skipping.")
                database.log_step(run_id, "1. Fetch emails", "skipped", "No emails found", db_path=db_path)
                database.finish_run(run_id, "success", db_path=db_path)
                return None
            msg = f"Fetched {len(emails)} emails"
            logger.info(msg)
            database.log_step(run_id, "1. Fetch emails", "success", msg, db_path=db_path)
        except EmailFetchError as e:
            logger.error("Email fetch failed: %s", e)
            database.log_step(run_id, "1. Fetch emails", "failed", str(e), db_path=db_path)
            database.finish_run(run_id, "failed", str(e), db_path=db_path)
            raise

        # 2. Parse content
        logger.info("Step 2/3: Parsing and classifying email content (AI-assisted)...")
        database.log_step(run_id, "2. Parse content", "running", db_path=db_path)
        try:
            digest = content_parser.parse_emails(emails)
            if not digest.articles:
                logger.info("No articles extracted. Skipping.")
                database.log_step(
                    run_id, "2. Parse content", "skipped", "No articles extracted", db_path=db_path
                )
                database.finish_run(run_id, "success", db_path=db_path)
                return None
            msg = f"Parsed {len(digest.articles)} articles, {digest.total_words} words"
            logger.info(msg)
            database.log_step(run_id, "2. Parse content", "success", msg, db_path=db_path)
        except ContentParseError as e:
            logger.error("Content parsing failed: %s", e)
            database.log_step(run_id, "2. Parse content", "failed", str(e), db_path=db_path)
            database.finish_run(run_id, "failed", str(e), db_path=db_path)
            raise

        # 3. Compile digest and save to database
        logger.info("Step 3/3: Compiling AI-summarized digest...")
        database.log_step(run_id, "3. Compile digest", "running", db_path=db_path)
        try:
            compiled = digest_compiler.compile(digest, show=show)
            compiled.email_count = len(emails)

            # Check if this date's digest is locked (episode already uploaded)
            if database.has_episode(compiled.date, db_path=db_path):
                msg = f"Digest for {compiled.date} is locked (episode exists) — skipping save"
                logger.info(msg)
                database.log_step(run_id, "3. Compile digest", "skipped", msg, db_path=db_path)
                database.finish_run(run_id, "success", db_path=db_path)
                return compiled

            database.save_digest(
                date=compiled.date,
                markdown_text=compiled.text,
                article_count=compiled.article_count,
                total_words=compiled.total_words,
                topics_summary=compiled.topics_summary,
                rss_summary=compiled.rss_summary,
                email_count=compiled.email_count,
                segment_counts=compiled.segment_counts,
                segment_sources=compiled.segment_sources,
                db_path=db_path,
            )

            msg = (
                f"Compiled {compiled.article_count} articles, "
                f"{compiled.total_words} words, saved to DB"
            )
            logger.info(msg)
            database.log_step(run_id, "3. Compile digest", "success", msg, db_path=db_path)
        except DigestCompileError as e:
            logger.error("Digest compilation failed: %s", e)
            database.log_step(run_id, "3. Compile digest", "failed", str(e), db_path=db_path)
            database.finish_run(run_id, "failed", str(e), db_path=db_path)
            raise

        database.finish_run(run_id, "success", db_path=db_path)
        logger.info("Digest ready for %s. Upload MP3 after NotebookLM.", compiled.date)
        return compiled

    except NoctuaError:
        raise
    except Exception as e:
        database.log_step(run_id, "Unexpected error", "failed", str(e), db_path=db_path)
        database.finish_run(run_id, "failed", str(e), db_path=db_path)
        raise


def main() -> None:
    """Entry point for digest preparation (steps 1-3)."""
    from config import shows

    # Use the first configured show for CLI invocation
    show = next(iter(shows.values())) if shows else None
    try:
        asyncio.run(generate_digest_only(show=show))
    except NoctuaError as e:
        logger.error("Pipeline failed: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Generation interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
