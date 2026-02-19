"""Orchestrator — daily podcast generation pipeline."""

import asyncio
import logging
import sys
import uuid

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


async def generate_digest_only() -> CompiledDigest | None:
    """Run steps 1-3 of the pipeline: fetch emails, parse, compile digest.

    Returns:
        The compiled digest, or None if no emails/articles were found.
    """
    run_id = uuid.uuid4().hex[:12]
    database.start_run(run_id)

    try:
        # 1. Fetch emails
        logger.info("Step 1/3: Fetching today's emails...")
        database.log_step(run_id, "1. Fetch emails", "running")
        try:
            emails = email_fetcher.fetch_todays_emails()
            if not emails:
                logger.info("No newsletters found. Skipping.")
                database.log_step(run_id, "1. Fetch emails", "skipped", "No newsletters found")
                database.finish_run(run_id, "success")
                return None
            msg = f"Fetched {len(emails)} emails"
            logger.info(msg)
            database.log_step(run_id, "1. Fetch emails", "success", msg)
        except EmailFetchError as e:
            logger.error("Email fetch failed: %s", e)
            database.log_step(run_id, "1. Fetch emails", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 2. Parse content
        logger.info("Step 2/3: Parsing email content...")
        database.log_step(run_id, "2. Parse content", "running")
        try:
            digest = content_parser.parse_emails(emails)
            if not digest.articles:
                logger.info("No articles extracted. Skipping.")
                database.log_step(
                    run_id, "2. Parse content", "skipped", "No articles extracted"
                )
                database.finish_run(run_id, "success")
                return None
            msg = f"Parsed {len(digest.articles)} articles, {digest.total_words} words"
            logger.info(msg)
            database.log_step(run_id, "2. Parse content", "success", msg)
        except ContentParseError as e:
            logger.error("Content parsing failed: %s", e)
            database.log_step(run_id, "2. Parse content", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 3. Compile digest and save to database
        logger.info("Step 3/3: Compiling digest...")
        database.log_step(run_id, "3. Compile digest", "running")
        try:
            compiled = digest_compiler.compile(digest)

            # Check if this date's digest is locked (episode already uploaded)
            if database.has_episode(compiled.date):
                msg = f"Digest for {compiled.date} is locked (episode exists) — skipping save"
                logger.info(msg)
                database.log_step(run_id, "3. Compile digest", "skipped", msg)
                database.finish_run(run_id, "success")
                return compiled

            database.save_digest(
                date=compiled.date,
                markdown_text=compiled.text,
                article_count=compiled.article_count,
                total_words=compiled.total_words,
                topics_summary=compiled.topics_summary,
                rss_summary=compiled.rss_summary,
                segment_counts=compiled.segment_counts,
                segment_sources=compiled.segment_sources,
            )

            msg = (
                f"Compiled {compiled.article_count} articles, "
                f"{compiled.total_words} words, saved to DB"
            )
            logger.info(msg)
            database.log_step(run_id, "3. Compile digest", "success", msg)
        except DigestCompileError as e:
            logger.error("Digest compilation failed: %s", e)
            database.log_step(run_id, "3. Compile digest", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        database.finish_run(run_id, "success")
        logger.info("Digest ready for %s. Upload MP3 after NotebookLM.", compiled.date)
        return compiled

    except NoctuaError:
        raise
    except Exception as e:
        database.log_step(run_id, "Unexpected error", "failed", str(e))
        database.finish_run(run_id, "failed", str(e))
        raise


def main() -> None:
    """Entry point for digest preparation (steps 1-3)."""
    try:
        asyncio.run(generate_digest_only())
    except NoctuaError as e:
        logger.error("Pipeline failed: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Generation interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
