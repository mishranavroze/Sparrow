"""NotebookLM login helper — sets up persistent Chrome session.

Run in the Replit Shell:
    source .venv/bin/activate && python scripts/notebooklm_login.py

At each step, a screenshot is saved to output/debug/login.png.
View it in the Replit file browser to see the browser state.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from config import settings

SCREENSHOT_PATH = Path("output/debug/login.png")


async def take_screenshot(page, label=""):
    SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(SCREENSHOT_PATH))
    if label:
        print(f"  [Screenshot saved: {label}]")
    print(f"  View at: {SCREENSHOT_PATH}")


async def main():
    user_data_dir = str(Path(settings.chrome_user_data_dir).expanduser())
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print("NOTEBOOKLM LOGIN HELPER")
    print("=" * 60)
    print()
    print("This will open Chrome and help you log into Google.")
    print(f"Screenshots are saved to: {SCREENSHOT_PATH}")
    print("View them in the Replit file browser as we go.")
    print()

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
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.set_default_timeout(15000)

        try:
            # Step 1: Go to Google sign-in
            print("Navigating to Google sign-in...")
            await page.goto(
                "https://accounts.google.com/signin",
                wait_until="networkidle",
                timeout=30000,
            )
            await asyncio.sleep(2)
            await take_screenshot(page, "Google sign-in page")

            current_url = page.url
            print(f"  Current URL: {current_url}")

            # Check if already logged in
            if "myaccount.google.com" in current_url or "accounts.google.com/b/" in current_url:
                print()
                print("Already logged in! Skipping to NotebookLM...")
            else:
                # Step 2: Enter email
                print()
                email = input("Enter your Google email: ").strip()
                email_input = await page.wait_for_selector(
                    'input[type="email"]', timeout=10000
                )
                await email_input.fill(email)
                await page.click("#identifierNext")
                await asyncio.sleep(3)
                await take_screenshot(page, "After email entry")

                # Step 3: Enter password
                print()
                password = input("Enter your Google password: ").strip()
                try:
                    password_input = await page.wait_for_selector(
                        'input[type="password"]', timeout=10000
                    )
                    await password_input.fill(password)
                    await page.click("#passwordNext")
                    await asyncio.sleep(5)
                    await take_screenshot(page, "After password entry")
                except Exception as e:
                    print(f"  Note: {e}")
                    await take_screenshot(page, "Password step issue")

                # Step 4: Check for 2FA
                current_url = page.url
                print(f"  Current URL: {current_url}")

                if "challenge" in current_url or "signin/v2" in current_url:
                    await take_screenshot(page, "2FA challenge")
                    print()
                    print("2FA verification required!")
                    print("Check the screenshot to see what type.")
                    code = input("Enter 2FA code (or press Enter to check phone): ").strip()
                    if code:
                        try:
                            code_input = await page.wait_for_selector(
                                'input[type="tel"], input[type="text"], '
                                'input[name="totpPin"]',
                                timeout=5000,
                            )
                            await code_input.fill(code)
                            # Try clicking Next/Verify
                            for selector in ["#totpNext", "button:has-text('Next')",
                                             "button:has-text('Verify')"]:
                                try:
                                    btn = await page.wait_for_selector(
                                        selector, timeout=2000
                                    )
                                    await btn.click()
                                    break
                                except Exception:
                                    continue
                            await asyncio.sleep(5)
                        except Exception as e:
                            print(f"  2FA input issue: {e}")
                    else:
                        print("  Waiting 30s for phone approval...")
                        await asyncio.sleep(30)

                    await take_screenshot(page, "After 2FA")

            # Step 5: Navigate to NotebookLM
            print()
            print("Navigating to NotebookLM...")
            await page.goto(
                "https://notebooklm.google.com",
                wait_until="networkidle",
                timeout=30000,
            )
            await asyncio.sleep(3)
            await take_screenshot(page, "NotebookLM page")

            current_url = page.url
            print(f"  Current URL: {current_url}")

            if "accounts.google.com" in current_url:
                print()
                print("ERROR: Not logged in — redirected to login page.")
                print("Google may have blocked automated login.")
                print("See ALTERNATIVE approach below.")
            else:
                print()
                print("=" * 60)
                print("SUCCESS! Logged into NotebookLM.")
                print("=" * 60)
                print()
                print(f"Session saved to: {user_data_dir}")

                # Try to create or find a notebook
                notebook_url = settings.notebooklm_notebook_url
                if not notebook_url:
                    print()
                    print("No NOTEBOOKLM_NOTEBOOK_URL set.")
                    print(f"Current URL: {current_url}")
                    print("If you see a notebook, copy its URL.")
                    print("Or create one at notebooklm.google.com and set it in .env")

        except Exception as e:
            await take_screenshot(page, f"Error: {e}")
            print(f"Error: {e}")
            raise
        finally:
            await browser.close()

    print()
    print("-" * 60)
    print("ALTERNATIVE: Manual cookie import")
    print("-" * 60)
    print("If automated login was blocked by Google:")
    print("1. Open notebooklm.google.com in YOUR browser")
    print("2. Create a notebook, copy the URL")
    print("3. Set NOTEBOOKLM_NOTEBOOK_URL in .env")
    print("4. We'll handle session issues at runtime")
    print()


if __name__ == "__main__":
    asyncio.run(main())
