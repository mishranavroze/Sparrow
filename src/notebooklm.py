"""Playwright automation for NotebookLM Audio Overview generation."""

import asyncio
import logging
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config import settings
from src.exceptions import (
    AudioGenerationTimeoutError,
    NotebookLMError,
    SelectorNotFoundError,
    SessionExpiredError,
)
from src.models import CompiledDigest

logger = logging.getLogger(__name__)

# Timeouts
NAVIGATION_TIMEOUT = 30_000  # 30s
ELEMENT_TIMEOUT = 10_000  # 10s
AUDIO_GENERATION_TIMEOUT = 900  # 15 minutes in seconds
AUDIO_POLL_INTERVAL = 15  # seconds


class NotebookLMAutomator:
    """Automates NotebookLM Audio Overview generation via Playwright."""

    async def generate_episode(self, digest: CompiledDigest) -> Path:
        """Full pipeline: upload digest, generate audio, download MP3.

        Args:
            digest: The compiled daily digest to upload.

        Returns:
            Path to the downloaded MP3 file.
        """
        user_data_dir = str(Path(settings.chrome_user_data_dir).expanduser())
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        # Ensure debug screenshot dir exists
        Path("output/debug").mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=True,
                viewport={"width": 1280, "height": 720},
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()
            page.set_default_timeout(ELEMENT_TIMEOUT)

            try:
                # Step 1: Navigate to notebook
                await self._navigate_to_notebook(page)

                # Step 2: Check session is valid
                await self._check_session(page)

                # Step 3: Clear previous sources (if reusing notebook)
                await self._clear_sources(page)

                # Step 4: Add digest as source text
                await self._add_text_source(page, digest.text)

                # Step 5: Wait for source processing
                await asyncio.sleep(3)

                # Step 6: Generate Audio Overview
                await self._generate_audio_overview(page)

                # Step 7: Wait for generation to complete
                await self._wait_for_audio_ready(page)

                # Step 8: Download the MP3
                mp3_path = await self._download_audio(page, digest.date)

                logger.info("Episode generated successfully: %s", mp3_path)
                return mp3_path

            except (NotebookLMError, SessionExpiredError):
                try:
                    await page.screenshot(path=f"output/debug/error-{digest.date}.png")
                except Exception:
                    pass
                raise

            except Exception as e:
                try:
                    await page.screenshot(path=f"output/debug/error-{digest.date}.png")
                except Exception:
                    pass
                raise NotebookLMError(f"Audio generation failed: {e}") from e

            finally:
                await browser.close()

    async def _find_element(
        self, page: Page, selectors: list[str], description: str, timeout: int = ELEMENT_TIMEOUT
    ):
        """Try multiple selectors to find an element.

        Args:
            page: Playwright page.
            selectors: List of CSS/text selectors to try.
            description: Human-readable description for error messages.
            timeout: Timeout per selector attempt in ms.

        Returns:
            The found element handle.

        Raises:
            SelectorNotFoundError: If no selector matches.
        """
        for selector in selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=timeout)
                if element:
                    return element
            except Exception:
                continue

        raise SelectorNotFoundError(
            f"Could not find {description} with any selector: {selectors}"
        )

    async def _navigate_to_notebook(self, page: Page) -> None:
        """Navigate to the NotebookLM notebook URL."""
        notebook_url = settings.notebooklm_notebook_url
        if not notebook_url:
            # Go to NotebookLM homepage to create a new notebook
            notebook_url = "https://notebooklm.google.com"

        logger.info("Navigating to %s", notebook_url)
        await page.goto(notebook_url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT)
        await asyncio.sleep(2)

    async def _check_session(self, page: Page) -> None:
        """Check if the Google session is still valid.

        Raises:
            SessionExpiredError: If redirected to login page.
        """
        current_url = page.url
        login_indicators = [
            "accounts.google.com/v3/signin",
            "accounts.google.com/ServiceLogin",
            "accounts.google.com/o/oauth2",
        ]
        for indicator in login_indicators:
            if indicator in current_url:
                raise SessionExpiredError(
                    "Google session expired. Please re-login manually with headless=False. "
                    f"Current URL: {current_url}"
                )

        logger.info("Session is valid. Current URL: %s", current_url)

    async def _clear_sources(self, page: Page) -> None:
        """Clear all existing sources from the notebook."""
        logger.info("Clearing existing sources...")

        # Look for source entries using various selectors
        source_selectors = [
            "source-entry",
            "[data-source-entry]",
            ".source-list-view source-entry",
        ]

        for attempt in range(20):  # max 20 sources to clear
            source = None
            for selector in source_selectors:
                try:
                    source = await page.wait_for_selector(selector, timeout=2000)
                    if source:
                        break
                except Exception:
                    continue

            if not source:
                break

            # Try to find and click the more/delete option on the source
            try:
                # Right-click or find the menu button on the source
                more_btn = await self._find_element(
                    page,
                    [
                        "source-entry button[aria-label='More options']",
                        "source-entry button[aria-label='Delete source']",
                        "source-entry .more-button",
                        "source-entry button:last-child",
                    ],
                    "source more button",
                    timeout=3000,
                )
                await more_btn.click()
                await asyncio.sleep(0.5)

                # Click delete in the menu
                delete_item = await self._find_element(
                    page,
                    [
                        "[role='menuitem']:has-text('Delete')",
                        "button:has-text('Delete')",
                        "[aria-label='Delete']",
                    ],
                    "delete menu item",
                    timeout=3000,
                )
                await delete_item.click()
                await asyncio.sleep(0.5)

                # Confirm deletion if dialog appears
                try:
                    confirm_btn = await page.wait_for_selector(
                        "button:has-text('Delete'), button:has-text('Confirm'), "
                        "button:has-text('Remove')",
                        timeout=2000,
                    )
                    if confirm_btn:
                        await confirm_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

            except SelectorNotFoundError:
                logger.warning("Could not find delete controls for source, skipping")
                break

        logger.info("Sources cleared")

    async def _add_text_source(self, page: Page, text: str) -> None:
        """Add the digest text as a source via clipboard/paste.

        Args:
            page: Playwright page.
            text: The digest text content.
        """
        logger.info("Adding text source (%d chars)...", len(text))

        # Click "Add source" button
        add_btn = await self._find_element(
            page,
            [
                "button:has-text('Add source')",
                "button:has-text('Add')",
                "[aria-label='Add source']",
                ".add-source-button",
                "button.add-source",
            ],
            "add source button",
        )
        await add_btn.click()
        await asyncio.sleep(1)

        # Select "Copied text" / "Text" source type
        text_option = await self._find_element(
            page,
            [
                "button:has-text('Copied text')",
                "button:has-text('Text')",
                "[aria-label='Copied text']",
                "[data-source-type='text']",
                ":text('Copied text')",
                "div:has-text('Copied text')",
            ],
            "text source option",
        )
        await text_option.click()
        await asyncio.sleep(1)

        # Find the text input area and fill it
        text_input = await self._find_element(
            page,
            [
                "textarea",
                "[contenteditable='true']",
                "div[role='textbox']",
                ".text-input textarea",
                "input[type='text']",
            ],
            "text input area",
        )

        # Use fill for textarea, or evaluate for contenteditable
        try:
            await text_input.fill(text)
        except Exception:
            await page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.selector);
                    if (el) {
                        el.textContent = args.text;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                }""",
                {"selector": "textarea, [contenteditable='true']", "text": text},
            )

        await asyncio.sleep(1)

        # Click the insert/save/submit button
        submit_btn = await self._find_element(
            page,
            [
                "button:has-text('Insert')",
                "button:has-text('Add')",
                "button:has-text('Save')",
                "button:has-text('Submit')",
                "button[type='submit']",
            ],
            "submit/insert button",
        )
        await submit_btn.click()
        await asyncio.sleep(2)

        logger.info("Text source added")

    async def _generate_audio_overview(self, page: Page) -> None:
        """Trigger Audio Overview generation."""
        logger.info("Starting Audio Overview generation...")

        # Navigate to the Audio Overview / Studio section
        audio_tab = await self._find_element(
            page,
            [
                "button:has-text('Audio Overview')",
                "button:has-text('Notebook guide')",
                "[aria-label='Audio Overview']",
                "button:has-text('Generate')",
                ".notebook-guide-button",
            ],
            "audio overview tab/button",
        )
        await audio_tab.click()
        await asyncio.sleep(2)

        # Click Generate button
        generate_btn = await self._find_element(
            page,
            [
                "button:has-text('Generate')",
                "button:has-text('Create')",
                "button:has-text('Generate audio')",
                "[aria-label='Generate']",
                "button:has-text('Deep Dive')",
            ],
            "generate audio button",
        )
        await generate_btn.click()
        await asyncio.sleep(2)

        logger.info("Audio generation triggered")

    async def _wait_for_audio_ready(self, page: Page) -> None:
        """Wait for audio generation to complete.

        Raises:
            AudioGenerationTimeoutError: If generation exceeds timeout.
        """
        logger.info(
            "Waiting for audio generation (timeout: %ds)...", AUDIO_GENERATION_TIMEOUT
        )
        elapsed = 0

        while elapsed < AUDIO_GENERATION_TIMEOUT:
            # Check for completion indicators
            try:
                # Look for play button or audio player (indicates completion)
                play_btn = await page.wait_for_selector(
                    "button[aria-label='Play'], "
                    "button[aria-label='play_arrow'], "
                    "audio, "
                    "[aria-label='Play audio'], "
                    ".audio-player button",
                    timeout=AUDIO_POLL_INTERVAL * 1000,
                )
                if play_btn:
                    logger.info("Audio generation complete (elapsed: %ds)", elapsed)
                    return
            except Exception:
                pass

            # Check for error indicators
            try:
                error_el = await page.query_selector(
                    ":text('error'), :text('failed'), :text('try again')"
                )
                if error_el:
                    error_text = await error_el.inner_text()
                    raise NotebookLMError(f"Audio generation failed: {error_text}")
            except NotebookLMError:
                raise
            except Exception:
                pass

            elapsed += AUDIO_POLL_INTERVAL
            if elapsed % 60 == 0:
                logger.info("Still generating... (%ds elapsed)", elapsed)

        raise AudioGenerationTimeoutError(
            f"Audio generation timed out after {AUDIO_GENERATION_TIMEOUT}s"
        )

    async def _download_audio(self, page: Page, date: str) -> Path:
        """Download the generated audio file.

        Args:
            page: Playwright page.
            date: Date string (YYYY-MM-DD) for the filename.

        Returns:
            Path to the saved MP3 file.
        """
        download_path = Path(f"output/episodes/noctua-{date}.mp3")
        download_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading audio...")

        # Try to find the more/download menu
        try:
            more_btn = await self._find_element(
                page,
                [
                    "button[aria-label='More options']",
                    "button[aria-label='more_vert']",
                    ".audio-player button[aria-label='More']",
                    "artifact-library button[aria-label='More options']",
                ],
                "more options button",
                timeout=5000,
            )
            await more_btn.click()
            await asyncio.sleep(0.5)

            # Click download in menu
            async with page.expect_download(timeout=60000) as download_info:
                download_btn = await self._find_element(
                    page,
                    [
                        "[role='menuitem']:has-text('Download')",
                        "button:has-text('Download')",
                        "[aria-label='Download']",
                    ],
                    "download menu item",
                    timeout=5000,
                )
                await download_btn.click()

            download = await download_info.value
            await download.save_as(str(download_path))

        except SelectorNotFoundError:
            # Fallback: try direct download button
            logger.info("Trying direct download button...")
            async with page.expect_download(timeout=60000) as download_info:
                download_btn = await self._find_element(
                    page,
                    [
                        "button:has-text('Download')",
                        "[aria-label='Download']",
                        "a[download]",
                        "button[aria-label='Download audio']",
                    ],
                    "download button",
                    timeout=10000,
                )
                await download_btn.click()

            download = await download_info.value
            await download.save_as(str(download_path))

        file_size = download_path.stat().st_size
        logger.info("Audio downloaded: %s (%.1f MB)", download_path, file_size / (1024 * 1024))

        return download_path
