"""NotebookLM login helper — sets up persistent Chrome session.

Reads GOOGLE_ACCOUNT_EMAIL from .env and checks/establishes
a Google session for NotebookLM.

If GOOGLE_ACCOUNT_PASSWORD is set, attempts automated login.
Otherwise, checks the existing session and provides instructions
if re-login is needed.

Screenshots are saved to output/debug/ at each step so you can
verify progress via the Replit file browser.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from config import settings

SCREENSHOT_DIR = Path("output/debug")

# Nix-provided Chromium
NIX_CHROMIUM_PATH = (
    "/nix/store/kcvsxrmgwp3ffz5jijyy7wn9fcsjl4hz-playwright-browsers-1.55.0-with-cjk"
    "/chromium-1187/chrome-linux/chrome"
)


async def screenshot(page, name):
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"login-{name}.png"
    await page.screenshot(path=str(path))
    print(f"  [Screenshot: {path}]")


async def check_session(browser):
    """Check if the current session can access NotebookLM."""
    page = browser.pages[0] if browser.pages else await browser.new_page()
    page.set_default_timeout(15000)

    print("Checking existing session...")
    await page.goto(
        "https://notebooklm.google.com",
        wait_until="networkidle",
        timeout=30000,
    )
    await asyncio.sleep(3)
    await screenshot(page, "session-check")

    current_url = page.url
    print(f"  URL: {current_url}")

    if "accounts.google.com" in current_url:
        return False, page
    return True, page


async def automated_login(page, email, password):
    """Attempt automated login with email and password."""
    try:
        # Step 1: Navigate to Google sign-in
        print("Step 1: Navigating to Google sign-in...")
        await page.goto(
            "https://accounts.google.com/signin",
            wait_until="networkidle",
            timeout=30000,
        )
        await asyncio.sleep(2)
        await screenshot(page, "01-signin-page")

        current_url = page.url
        print(f"  URL: {current_url}")

        # Check if already logged in
        if "myaccount.google.com" in current_url or "accounts.google.com/b/" in current_url:
            print("  Already logged in!")
        else:
            # Step 2: Enter email
            print("Step 2: Entering email...")
            email_input = await page.wait_for_selector(
                'input[type="email"]', timeout=10000
            )
            await email_input.fill(email)
            await page.click("#identifierNext")
            await asyncio.sleep(3)
            await screenshot(page, "02-after-email")
            print(f"  URL: {page.url}")

            # Step 3: Enter password
            print("Step 3: Entering password...")
            try:
                password_input = await page.wait_for_selector(
                    'input[type="password"]', timeout=10000
                )
                await password_input.fill(password)
                await page.click("#passwordNext")
                await asyncio.sleep(5)
                await screenshot(page, "03-after-password")
                print(f"  URL: {page.url}")
            except Exception as e:
                await screenshot(page, "03-password-error")
                print(f"  Password step issue: {e}")

            # Step 4: Check for 2FA or other challenges
            current_url = page.url
            if "challenge" in current_url or "signin/v2" in current_url:
                await screenshot(page, "04-2fa-challenge")
                print()
                print("=" * 60)
                print("2FA REQUIRED")
                print("=" * 60)
                print("Google is asking for 2FA verification.")
                print("Check screenshot: output/debug/login-04-2fa-challenge.png")
                print()
                print("This script cannot complete 2FA automatically.")
                print("Options:")
                print("  1. Approve on your phone if it's a push notification")
                print("  2. Disable 2FA temporarily, re-run this script")
                print()
                # Wait a bit in case it's a phone push
                print("Waiting 30s for phone approval...")
                await asyncio.sleep(30)
                await screenshot(page, "04-after-2fa-wait")
                print(f"  URL: {page.url}")

        # Step 5: Navigate to NotebookLM
        print()
        print("Step 5: Navigating to NotebookLM...")
        await page.goto(
            "https://notebooklm.google.com",
            wait_until="networkidle",
            timeout=30000,
        )
        await asyncio.sleep(3)
        await screenshot(page, "05-notebooklm")

        current_url = page.url
        print(f"  URL: {current_url}")

        if "accounts.google.com" in current_url:
            return False
        return True

    except Exception as e:
        await screenshot(page, "error")
        print(f"Error during login: {e}")
        return False


async def main():
    email = settings.google_account_email
    password = settings.google_account_password

    user_data_dir = str(Path(settings.chrome_user_data_dir).expanduser())
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    executable = NIX_CHROMIUM_PATH if Path(NIX_CHROMIUM_PATH).exists() else None

    print()
    print("=" * 60)
    print("NOTEBOOKLM SESSION CHECK")
    print("=" * 60)
    if email:
        print(f"  Email: {email}")
    print(f"  Chrome profile: {user_data_dir}")
    print(f"  Password configured: {'yes' if password else 'no'}")
    print()

    async with async_playwright() as p:
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
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        try:
            # First, check if the existing session works
            session_ok, page = await check_session(browser)

            if session_ok:
                print()
                print("=" * 60)
                print("SESSION ACTIVE — NotebookLM is accessible.")
                print("=" * 60)
                print("  The pipeline can run without re-login.")
                await browser.close()
                return

            print()
            print("Session expired — login required.")
            print()

            if email and password:
                # Attempt automated login
                print("Attempting automated login...")
                success = await automated_login(page, email, password)

                if success:
                    print()
                    print("=" * 60)
                    print("SUCCESS! Logged into NotebookLM.")
                    print("=" * 60)
                    print(f"  Session saved to: {user_data_dir}")
                    print("  The pipeline can now run headless.")
                    await browser.close()
                    return

                print()
                print("Automated login failed.")

            # No password or automated login failed — provide instructions
            print()
            print("=" * 60)
            print("MANUAL LOGIN REQUIRED")
            print("=" * 60)
            print()
            print("The Google session has expired and needs to be refreshed.")
            print()
            print("To fix this, set these environment variables and re-run:")
            print()
            print("  GOOGLE_ACCOUNT_EMAIL=your-email@gmail.com")
            print("  GOOGLE_ACCOUNT_PASSWORD=your-password")
            print()
            print("Then run:")
            print("  python scripts/notebooklm_login.py")
            print()
            print("Note: If your account uses 2FA, you may need to approve")
            print("the login on your phone when prompted.")
            print()
            print("Check screenshots in output/debug/ for current session state.")

            await browser.close()
            sys.exit(1)

        except Exception as e:
            await screenshot(page if 'page' in dir() else browser.pages[0], "error")
            print(f"Error: {e}")
            await browser.close()
            raise


if __name__ == "__main__":
    asyncio.run(main())
