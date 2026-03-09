"""
Interactive Playwright browser session.

Run from the Ubuntu terminal (not WSL VSCode terminal):
    source .venv/bin/activate
    python scripts/play_browser.py [URL]

Opens a visible Chromium window with the same stealth settings used by the
scraper. The browser stays open until you press Enter in this terminal.

Examples:
    python scripts/play_browser.py
    python scripts/play_browser.py https://novellive.app
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

DEFAULT_URL = "https://freewebnovel.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Chrome launch args that suppress automation signals
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
]

# Injected before every page load — removes the fingerprints CF checks for
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False, slow_mo=50, args=LAUNCH_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["geolocation"],
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()

        print(f"Opening {url} …")
        page.goto(url, wait_until="domcontentloaded")
        print("Browser is open.")
        print()
        print("You can now interact with the browser window.")
        print("Come back here and press Enter to close it.")
        print()

        input("[ Press Enter to close the browser ]")

        # Print cookies before closing — handy for debugging.
        cookies = context.cookies()
        if cookies:
            print("\nCookies collected:")
            for c in cookies:
                print(
                    f"  {c['name']}={c['value'][:40]}…"
                    if len(c["value"]) > 40
                    else f"  {c['name']}={c['value']}"
                )

        browser.close()
        print("Browser closed.")


if __name__ == "__main__":
    main()
