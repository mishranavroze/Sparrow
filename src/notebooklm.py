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

# Nix-provided Chromium (has all required system libs)
NIX_CHROMIUM_PATH = (
    "/nix/store/kcvsxrmgwp3ffz5jijyy7wn9fcsjl4hz-playwright-browsers-1.55.0-with-cjk"
    "/chromium-1187/chrome-linux/chrome"
)


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
            # Use Nix-provided Chromium if available, fall back to default
            executable = NIX_CHROMIUM_PATH if Path(NIX_CHROMIUM_PATH).exists() else None

            browser = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=executable,
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

                # Step 2.5: Dismiss any overlay dialogs
                await self._dismiss_dialogs(page)

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
        await page.goto(notebook_url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(5)

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

    async def _dismiss_dialogs(self, page: Page) -> None:
        """Dismiss any overlay dialogs that appear on page load."""
        logger.info("Checking for overlay dialogs...")

        # Try multiple close strategies for common NotebookLM popups
        close_selectors = [
            "button[aria-label='Close']",
            ".cdk-overlay-container button[aria-label='Close']",
            "button.close-button",
            "mat-dialog-container button[aria-label='Close']",
            ".cdk-overlay-backdrop",
        ]

        for selector in close_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3000)
                if el:
                    await el.click()
                    logger.info("Dismissed dialog via: %s", selector)
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue

        # Also press Escape as a fallback
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
        except Exception:
            pass

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
        """Add the digest text as a source via file upload.

        Saves text to a temp file and uploads it, bypassing Angular textarea issues.

        Args:
            page: Playwright page.
            text: The digest text content.
        """
        logger.info("Adding text source (%d chars) via file upload...", len(text))

        # Save digest to a temporary text file
        import tempfile
        tmp_file = Path(tempfile.mktemp(suffix=".txt", prefix="noctua-digest-"))
        tmp_file.write_text(text, encoding="utf-8")
        logger.info("Saved digest to temp file: %s (%d bytes)", tmp_file, tmp_file.stat().st_size)

        try:
            # Dismiss any existing dialogs first
            await self._dismiss_dialogs(page)

            # Click "+ Add sources" button via JS to open the source dialog
            await page.evaluate(
                """() => {
                    const buttons = [...document.querySelectorAll('button')];
                    const btn = buttons.find(b =>
                        b.textContent.includes('Add source') || b.textContent.includes('Upload a source')
                    );
                    if (btn) btn.click();
                }"""
            )
            await asyncio.sleep(3)
            await page.screenshot(path="output/debug/add-source-dialog.png")

            # Click "Upload files" button using Playwright's force click
            # (force=True bypasses overlay intercept while preserving native events)
            logger.info("Clicking 'Upload files' with force click...")
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await page.locator("button:has-text('Upload files')").click(force=True)
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(tmp_file))
            logger.info("File uploaded via file chooser")

            await asyncio.sleep(5)
            await page.screenshot(path="output/debug/after-upload.png")

            # Wait for source to be processed (check for source count change)
            for i in range(12):  # Wait up to 60s
                source_count = await page.evaluate(
                    """() => {
                        const el = document.querySelector('[class*="source"]');
                        const countEl = [...document.querySelectorAll('*')].find(
                            e => e.textContent.match(/^\\d+ source/) && e.children.length === 0
                        );
                        return countEl ? countEl.textContent.trim() : 'unknown';
                    }"""
                )
                logger.info("Source count check %d: %s", i + 1, source_count)
                if "1 source" in source_count or "2 source" in source_count:
                    break
                await asyncio.sleep(5)

            await page.screenshot(path="output/debug/after-source-processing.png")
            logger.info("Text source added via file upload")

        finally:
            # Clean up temp file
            try:
                tmp_file.unlink()
            except Exception:
                pass

    async def _generate_audio_overview(self, page: Page) -> None:
        """Trigger Audio Overview generation."""
        logger.info("Starting Audio Overview generation...")

        # Dismiss any lingering overlays first
        await self._dismiss_dialogs(page)

        await page.screenshot(path="output/debug/before-audio-gen.png")

        # Use JS clicks to bypass any remaining overlays
        # Step 1: Click Audio Overview in the Studio panel
        clicked = await page.evaluate(
            """() => {
                // Look for Audio Overview button/link in the Studio panel
                const allButtons = [...document.querySelectorAll('button, [role="tab"], [role="button"], a')];
                const audioBtn = allButtons.find(b =>
                    b.textContent.includes('Audio') &&
                    (b.textContent.includes('Overview') || b.textContent.includes('Audio...'))
                );
                if (audioBtn) { audioBtn.click(); return 'audio-overview'; }

                // Try "Notebook guide" as fallback
                const guideBtn = allButtons.find(b => b.textContent.includes('Notebook guide'));
                if (guideBtn) { guideBtn.click(); return 'notebook-guide'; }

                return 'not-found';
            }"""
        )
        logger.info("Audio tab click result: %s", clicked)
        await asyncio.sleep(3)

        await page.screenshot(path="output/debug/after-audio-tab.png")

        # Step 2: Click Generate button
        gen_result = await page.evaluate(
            """() => {
                const buttons = [...document.querySelectorAll('button')];
                // Prefer exact "Generate" button
                let btn = buttons.find(b => b.textContent.trim() === 'Generate');
                if (btn) { btn.click(); return 'generate'; }
                // Try variations
                btn = buttons.find(b => b.textContent.includes('Generate'));
                if (btn) { btn.click(); return 'generate-partial'; }
                btn = buttons.find(b => b.textContent.includes('Deep Dive'));
                if (btn) { btn.click(); return 'deep-dive'; }
                btn = buttons.find(b => b.textContent.trim() === 'Create');
                if (btn) { btn.click(); return 'create'; }
                return 'not-found';
            }"""
        )
        logger.info("Generate button click result: %s", gen_result)
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
            # Check for completion via JS — look for play button, but also ensure
            # no active "Generating" indicator is present
            status = await page.evaluate(
                """() => {
                    // First check if still generating
                    const allText = document.body.innerText;
                    const isGenerating = allText.includes('Generating Audio Overview');
                    if (isGenerating) {
                        return 'generating';
                    }

                    // Check for play button (multiple possible selectors)
                    const playBtns = document.querySelectorAll(
                        'button[aria-label="Play"], button[aria-label="play_arrow"], ' +
                        '[aria-label="Play audio"], button[aria-label="Pause"]'
                    );
                    if (playBtns.length > 0) return 'ready:play-button';

                    // Check for audio element
                    if (document.querySelector('audio')) return 'ready:audio-element';

                    // Check for error
                    if (allText.includes('generation failed') || allText.includes('try again'))
                        return 'error';

                    return 'unknown';
                }"""
            )

            if status.startswith("ready:"):
                logger.info("Audio generation complete: %s (elapsed: %ds)", status, elapsed)
                return

            if status == "error":
                raise NotebookLMError("Audio generation failed (error detected on page)")

            elapsed += AUDIO_POLL_INTERVAL

            if elapsed % 60 == 0:
                logger.info("Still generating... (%ds elapsed, status: %s)", elapsed, status)
                await page.screenshot(path=f"output/debug/audio-wait-{elapsed}s.png")

            await asyncio.sleep(AUDIO_POLL_INTERVAL)

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
        await page.screenshot(path="output/debug/before-download.png")

        # Find and click the three-dot menu (⋮) next to the audio entry
        try:
            # Use force click on the three-dot menu button
            # Try multiple selectors for the three-dot menu
            three_dot_selectors = [
                "button[aria-label='More actions']",
                "button[aria-label='More options']",
                "button[aria-label='more_vert']",
                "mat-icon:has-text('more_vert')",
            ]
            clicked_menu = False
            for selector in three_dot_selectors:
                try:
                    await page.locator(selector).last.click(force=True, timeout=3000)
                    clicked_menu = True
                    logger.info("Clicked three-dot menu via: %s", selector)
                    break
                except Exception:
                    continue

            if not clicked_menu:
                # Fallback: find the three-dot button via JS
                logger.info("Trying JS click for three-dot menu...")
                await page.evaluate(
                    """() => {
                        // Look for three-dot menu buttons (usually the last button in an audio row)
                        const btns = [...document.querySelectorAll('button')];
                        const menuBtn = btns.find(b => {
                            const icon = b.querySelector('mat-icon, .material-icons');
                            return icon && icon.textContent.trim() === 'more_vert';
                        });
                        if (menuBtn) menuBtn.click();
                    }"""
                )

            await asyncio.sleep(1)
            await page.screenshot(path="output/debug/after-menu-click.png")

            # Click download in the dropdown menu
            async with page.expect_download(timeout=60000) as download_info:
                download_clicked = await page.evaluate(
                    """() => {
                        const items = [...document.querySelectorAll(
                            '[role="menuitem"], button, a'
                        )];
                        const dlItem = items.find(i => i.textContent.includes('Download'));
                        if (dlItem) { dlItem.click(); return 'clicked'; }
                        return 'not-found';
                    }"""
                )
                logger.info("Download menu click result: %s", download_clicked)

            download = await download_info.value
            await download.save_as(str(download_path))

        except Exception as e:
            logger.info("Menu download failed (%s), checking for direct audio URL...", e)
            await page.screenshot(path="output/debug/download-failed.png")

            # Last resort: try to extract audio URL directly from the page
            audio_url = await page.evaluate(
                """() => {
                    const audio = document.querySelector('audio');
                    if (audio && audio.src) return audio.src;
                    const sources = document.querySelectorAll('source');
                    for (const s of sources) if (s.src) return s.src;
                    return null;
                }"""
            )
            if audio_url:
                logger.info("Found audio URL: %s", audio_url[:100])
                response = await page.request.get(audio_url)
                with open(str(download_path), "wb") as f:
                    f.write(await response.body())
            else:
                raise SelectorNotFoundError(f"Could not download audio: {e}") from e

        file_size = download_path.stat().st_size
        logger.info("Audio downloaded: %s (%.1f MB)", download_path, file_size / (1024 * 1024))

        return download_path
