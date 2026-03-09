from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import hrequests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from app.engine.rate import RateController

_COOKIE_CACHE = Path.home() / ".cache" / "webnovel-scraper" / "cookies.json"
_CHALLENGE_POLL_INTERVAL_MS = 1_500  # how often to check if challenge cleared
_CHALLENGE_TIMEOUT_S = 120  # give up after 2 minutes


@dataclass(frozen=True, slots=True)
class NetworkResult:
    url: str
    status_code: int
    text: str
    used_browser_fallback: bool


class NetworkClient:
    def __init__(
        self,
        timeout_seconds: float = 20.0,
        page_load_delay: float = 1.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.page_load_delay = page_load_delay
        self._rate_controllers: dict[str, RateController] = {}
        self._rc_lock = threading.Lock()
        self._cookies: dict[str, dict[str, str]] = self._load_cookies()
        self._cookie_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _load_cookies() -> dict[str, dict[str, str]]:
        try:
            return dict(json.loads(_COOKIE_CACHE.read_text()))
        except Exception:
            return {}

    def _save_cookies(self) -> None:
        _COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_CACHE.write_text(json.dumps(self._cookies, indent=2))

    def _domain_cookies(self, url: str) -> dict[str, str]:
        domain = urlparse(url).netloc.casefold()
        with self._cookie_lock:
            return dict(self._cookies.get(domain, {}))

    def _cookie_header(self, url: str) -> dict[str, str]:
        """Return a {"Cookie": "..."} header dict, or {} when no cookies saved."""
        cookies = self._domain_cookies(url)
        if not cookies:
            return {}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    def _store_cookies(self, url: str, cookies: dict[str, str]) -> None:
        domain = urlparse(url).netloc.casefold()
        with self._cookie_lock:
            self._cookies[domain] = cookies
            self._save_cookies()

    # ------------------------------------------------------------------
    # Rate control
    # ------------------------------------------------------------------

    def _get_rate_controller(self, url: str) -> RateController:
        domain = urlparse(url).netloc.casefold()
        with self._rc_lock:
            if domain not in self._rate_controllers:
                self._rate_controllers[domain] = RateController(domain)
            return self._rate_controllers[domain]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_text(
        self,
        url: str,
        method: str = "GET",
        data: dict[str, str] | None = None,
    ) -> NetworkResult:
        """Fetch *url* and return the response text.

        Parameters
        ----------
        method:
            HTTP verb — ``"GET"`` (default) or ``"POST"``.
        data:
            Form fields to send with a POST request.  Ignored for GET.
        """
        rc = self._get_rate_controller(url)
        last_status, last_text = 429, ""
        browser_used = False

        for attempt in range(3):
            rc.wait()
            # Pass cookies via header — hrequests has a bug where cookies= param
            # wraps a dict in a RequestsCookieJar and then tries to call .value on
            # the jar object itself, raising an AttributeError.
            headers = self._cookie_header(url)

            if method.upper() == "POST":
                response = hrequests.post(
                    url,
                    data=data or {},
                    headers=headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
            else:
                response = hrequests.get(
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )

            last_status = int(response.status_code)
            last_text = response.text

            if last_status == 429:
                rc.throttled()
                continue

            if self._is_bot_challenge(last_status, last_text):
                if attempt == 0:
                    # First encounter — open visible browser for user to solve.
                    rc.throttled()
                    result = self._solve_challenge_in_browser(url)
                    self._store_cookies(url, result["cookies"])
                    browser_used = True
                    # For GET we can return the browser content directly.
                    if method.upper() == "GET":
                        return NetworkResult(
                            url=url,
                            status_code=result["status_code"],
                            text=result["text"],
                            used_browser_fallback=True,
                        )
                    # For POST, loop again — hrequests will now carry the cookies.
                    continue
                # Subsequent bot-challenge after we already tried — give up.
                break

            rc.success()
            return NetworkResult(
                url=url,
                status_code=last_status,
                text=last_text,
                used_browser_fallback=browser_used,
            )

        return NetworkResult(
            url=url, status_code=last_status, text=last_text, used_browser_fallback=browser_used
        )

    def get_binary(self, url: str) -> bytes | None:
        rc = self._get_rate_controller(url)
        rc.wait()
        response = hrequests.get(
            url,
            headers=self._cookie_header(url),
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        status = int(response.status_code)
        if status == 429:
            rc.throttled()
            return None
        if status >= 400:
            return None
        rc.success()
        return bytes(response.content)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_bot_challenge(status_code: int, text: str) -> bool:
        """Return True when the response is a Cloudflare / bot-detection wall."""
        lowered = text.casefold()
        return status_code == 403 or any(
            token in lowered
            for token in (
                "cf-chl",
                "cloudflare",
                "attention required",
                "just a moment",
                "captcha",
            )
        )

    def _solve_challenge_in_browser(self, url: str) -> dict[str, Any]:
        """Open a visible browser so the user can pass a bot/Cloudflare challenge.

        Polls the page every 1.5 s until the challenge clears or the timeout
        expires, then extracts cookies and returns page content.
        """
        print(
            f"\n[webnovel-scraper] Bot challenge detected for {urlparse(url).netloc}.\n"
            "  A browser window will open — please wait for the challenge to clear\n"
            "  (usually 3-10 seconds).  Do not close the window.\n"
        )
        timeout_ms = int(self.timeout_seconds * 1000)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()

            status_code = 200
            try:
                response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if response is not None:
                    status_code = int(response.status)
            except PlaywrightTimeoutError:
                status_code = 408

            # Poll until the bot challenge disappears or we time out.
            elapsed_ms = 0
            while elapsed_ms < _CHALLENGE_TIMEOUT_S * 1000:
                page.wait_for_timeout(_CHALLENGE_POLL_INTERVAL_MS)
                elapsed_ms += _CHALLENGE_POLL_INTERVAL_MS
                current_text = page.content()
                if not self._is_bot_challenge(0, current_text):
                    # Challenge cleared — grab final status from the real page.
                    status_code = 200
                    break

            page.wait_for_timeout(int(self.page_load_delay * 1000))
            text = page.content()

            # Extract cookies as a plain {name: value} dict for hrequests.
            raw_cookies = context.cookies()
            cookies: dict[str, str] = {c["name"]: c["value"] for c in raw_cookies}

            browser.close()

        print("[webnovel-scraper] Challenge cleared. Cookies saved for future requests.\n")
        return {"status_code": status_code, "text": text, "cookies": cookies}
