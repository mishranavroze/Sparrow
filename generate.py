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
    episode_manager,
    feed_builder,
)
from src.exceptions import (
    ContentParseError,
    DigestCompileError,
    EmailFetchError,
    EpisodeProcessError,
    FeedBuildError,
    NoctuaError,
    NotebookLMError,
    SessionExpiredError,
)
from src.notebooklm import NotebookLMAutomator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def generate_episode() -> None:
    """Run the full podcast generation pipeline.

    Steps:
        1. Fetch today's newsletter emails from Gmail
        2. Parse email content into articles
        3. Compile articles into a single source document (saved to DB)
        4. Generate audio via NotebookLM (Playwright)
        5. Process episode (validate MP3, extract metadata)
        6. Update the RSS feed with the new episode
    """
    run_id = uuid.uuid4().hex[:12]
    database.start_run(run_id)

    try:
        # 1. Fetch emails
        logger.info("Step 1/6: Fetching today's emails...")
        database.log_step(run_id, "1. Fetch emails", "running")
        try:
            emails = email_fetcher.fetch_todays_emails()
            if not emails:
                logger.info("No newsletters today. Skipping generation.")
                database.log_step(run_id, "1. Fetch emails", "skipped", "No newsletters today")
                database.finish_run(run_id, "success")
                return
            msg = f"Fetched {len(emails)} emails"
            logger.info(msg)
            database.log_step(run_id, "1. Fetch emails", "success", msg)
        except EmailFetchError as e:
            logger.error("Email fetch failed: %s", e)
            database.log_step(run_id, "1. Fetch emails", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 2. Parse content
        logger.info("Step 2/6: Parsing email content...")
        database.log_step(run_id, "2. Parse content", "running")
        try:
            digest = content_parser.parse_emails(emails)
            if not digest.articles:
                logger.info("No articles extracted. Skipping generation.")
                database.log_step(
                    run_id, "2. Parse content", "skipped", "No articles extracted"
                )
                database.finish_run(run_id, "success")
                return
            msg = f"Parsed {len(digest.articles)} articles, {digest.total_words} words"
            logger.info(msg)
            database.log_step(run_id, "2. Parse content", "success", msg)
        except ContentParseError as e:
            logger.error("Content parsing failed: %s", e)
            database.log_step(run_id, "2. Parse content", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 3. Compile digest and save to database
        logger.info("Step 3/6: Compiling digest...")
        database.log_step(run_id, "3. Compile digest", "running")
        try:
            compiled = digest_compiler.compile(digest)

            # Save the compiled markdown to the database
            database.save_digest(
                date=compiled.date,
                markdown_text=compiled.text,
                article_count=compiled.article_count,
                total_words=compiled.total_words,
                topics_summary=compiled.topics_summary,
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

        # 4. Generate audio via NotebookLM
        logger.info("Step 4/6: Generating audio via NotebookLM...")
        database.log_step(run_id, "4. Generate audio (NotebookLM)", "running")
        try:
            automator = NotebookLMAutomator()
            mp3_path = await automator.generate_episode(compiled)
            msg = f"Audio generated: {mp3_path}"
            logger.info(msg)
            database.log_step(run_id, "4. Generate audio (NotebookLM)", "success", msg)
        except SessionExpiredError as e:
            logger.error("Google session expired: %s", e)
            logger.error("Please re-login manually by running with headless=False")
            database.log_step(
                run_id, "4. Generate audio (NotebookLM)", "failed",
                f"Session expired — manual re-login required: {e}",
            )
            database.finish_run(run_id, "failed", str(e))
            raise
        except NotebookLMError as e:
            logger.error("NotebookLM automation failed: %s", e)
            database.log_step(run_id, "4. Generate audio (NotebookLM)", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 5. Process episode
        logger.info("Step 5/6: Processing episode...")
        database.log_step(run_id, "5. Process episode", "running")
        try:
            metadata = episode_manager.process(mp3_path, compiled.topics_summary)
            msg = (
                f"Episode: {metadata.duration_formatted}, "
                f"{metadata.file_size_bytes / 1_000_000:.1f} MB"
            )
            logger.info(msg)
            database.log_step(run_id, "5. Process episode", "success", msg)
        except EpisodeProcessError as e:
            logger.error("Episode processing failed: %s", e)
            database.log_step(run_id, "5. Process episode", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        # 6. Update RSS feed
        logger.info("Step 6/6: Updating RSS feed...")
        database.log_step(run_id, "6. Update RSS feed", "running")
        try:
            feed_builder.add_episode(metadata)
            msg = "Feed updated. Episode published."
            logger.info(msg)
            database.log_step(run_id, "6. Update RSS feed", "success", msg)
        except FeedBuildError as e:
            logger.error("Feed building failed: %s", e)
            database.log_step(run_id, "6. Update RSS feed", "failed", str(e))
            database.finish_run(run_id, "failed", str(e))
            raise

        database.finish_run(run_id, "success")
        logger.info("Pipeline complete. Episode for %s is live.", compiled.date)

    except NoctuaError:
        # Already logged to DB above
        raise
    except Exception as e:
        database.log_step(run_id, "Unexpected error", "failed", str(e))
        database.finish_run(run_id, "failed", str(e))
        raise


def main() -> None:
    """Entry point for the generation pipeline."""
    try:
        asyncio.run(generate_episode())
    except NoctuaError as e:
        logger.error("Pipeline failed: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Generation interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
