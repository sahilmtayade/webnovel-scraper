from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from dataclasses import dataclass as _plain_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from app.engine.rate import (
    _DEFAULT_BACKOFF_FACTOR,
    _DEFAULT_RECOVERY_FACTOR,
    _PROXY_START_INTERVAL,
    RateController,
)

# Typed proxy files at the project root — bare host:port lines are auto-prefixed.
_ROOT = Path(__file__).resolve().parent.parent.parent
# How many different proxies to try per fetch before giving up.
_MAX_PROXY_TRIES = 8
# Warm-up probe: short timeout and a reliable HTTPS target.
_PROXY_PROBE_TIMEOUT: float = 8.0
_PROXY_PROBE_URL = "https://www.google.com"
# curl error codes that indicate a broken/dead proxy (not a server-side issue).
_PROXY_CURL_ERRORS = (
    "curl: (5)",  # CURLE_COULDNT_RESOLVE_PROXY
    "curl: (6)",  # CURLE_COULDNT_RESOLVE_HOST  (via broken proxy)
    "curl: (7)",  # CURLE_COULDNT_CONNECT
    "curl: (28)",  # CURLE_OPERATION_TIMEDOUT
    "curl: (35)",  # CURLE_SSL_CONNECT_ERROR / recv failure
    "curl: (43)",  # CURLE_BAD_FUNCTION_ARGUMENT — invalid response header from proxy
    "curl: (47)",  # CURLE_TOO_MANY_REDIRECTS (proxy loop)
    "curl: (56)",  # CURLE_RECV_ERROR — CONNECT tunnel failed
    "curl: (60)",  # CURLE_SSL_CACERT — SSL certificate problem
    "curl: (97)",  # CURLE_PROXY
)
_PROXY_FOLDER = _ROOT / "proxies"
_PROXY_FILES: list[tuple[Path, str]] = [
    (_PROXY_FOLDER / "proxies_http.txt", "http"),
    (_PROXY_FOLDER / "proxies_socks4.txt", "socks4"),
    (_PROXY_FOLDER / "proxies_socks5.txt", "socks5"),
]


@_plain_dataclass
class WorkerState:
    """Live status of one download thread, owned by NetworkClient."""

    worker_num: int  # 1-based display index
    label: str = ""  # chapter title currently being fetched
    state: str = "idle"  # "idle" | "fetch" | "sleep"
    sleep_until: float = 0.0  # monotonic deadline; 0 = not sleeping
    proxy_num: int | None = None  # 1-based proxy index used for the last successful request


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
        max_browser_sessions: int = 3,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        recovery_factor: float = _DEFAULT_RECOVERY_FACTOR,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.page_load_delay = page_load_delay
        self.max_browser_sessions = max_browser_sessions
        self.backoff_factor = backoff_factor
        self.recovery_factor = recovery_factor
        self._rate_controllers: dict[str, RateController] = {}
        self._rc_lock = threading.Lock()
        self._cookies: dict[str, dict[str, str]] = self._load_cookies()
        self._cookie_lock = threading.Lock()
        # Cap how many headed browser windows we open simultaneously.
        self._browser_sem = threading.Semaphore(max_browser_sessions)
        # Per-thread live status — keyed by threading.get_ident().
        self.worker_states: dict[int, WorkerState] = {}
        self._ws_lock = threading.Lock()
        self._worker_counter: int = 0
        # Proxy rotation — loaded once at startup from proxies.txt.
        self._proxies: list[str] = self._load_proxies()
        self._proxy_index: int = 0
        self._proxy_lock = threading.Lock()
        if self._proxies:
            print(f"[webnovel-scraper] Loaded {len(self._proxies)} proxies (http/socks4/socks5)")
            self._warm_proxies()

    # ------------------------------------------------------------------
    # Proxy helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_proxies() -> list[str]:
        """Read all typed proxy files and return a combined list of proxy URLs.

        Each file has an associated default scheme (http / socks4 / socks5).
        Lines that already contain ``://`` are used verbatim; bare ``host:port``
        entries are prefixed with the file's default scheme.
        Lines starting with ``#`` and blank lines are ignored.
        """
        proxies: list[str] = []
        for path, scheme in _PROXY_FILES:
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "://" in line:
                    proxies.append(line)
                else:
                    proxies.append(f"{scheme}://{line}")
        return proxies

    def _warm_proxies(self) -> None:
        """Test every loaded proxy in parallel and blacklist the dead ones.

        Spawns one thread per proxy so all probes run simultaneously.
        A proxy is kept if it reaches ``_PROXY_PROBE_URL`` without a
        transport error within ``_PROXY_PROBE_TIMEOUT`` seconds.
        Non-transport errors (e.g. HTTP 403 from the probe target) mean
        the proxy is alive and working — only curl connection failures
        are treated as dead.
        """
        total = len(self._proxies)
        print(
            f"[webnovel-scraper] Probing {total} proxies in parallel "
            f"(timeout {_PROXY_PROBE_TIMEOUT}s) …"
        )
        alive: list[int] = [0]
        alive_lock = threading.Lock()

        def _probe(proxy_url: str) -> None:
            proxy = {"http": proxy_url, "https": proxy_url}
            try:
                cffi_requests.get(
                    _PROXY_PROBE_URL,
                    timeout=_PROXY_PROBE_TIMEOUT,
                    allow_redirects=True,
                    impersonate="chrome120",
                    proxies=proxy,
                )
                with alive_lock:
                    alive[0] += 1
            except Exception as exc:
                if self._is_proxy_error(exc):
                    self._blacklist_proxy(proxy)
                else:
                    # Reachable but returned an unusual response — proxy is live.
                    with alive_lock:
                        alive[0] += 1

        snapshot = list(self._proxies)  # copy before any concurrent mutations
        threads = [threading.Thread(target=_probe, args=(p,), daemon=True) for p in snapshot]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        removed = total - alive[0]
        print(
            f"[webnovel-scraper] Proxy warm-up done: "
            f"{alive[0]}/{total} alive" + (f", {removed} removed" if removed else "") + "."
        )

    def _next_proxy(self) -> tuple[dict[str, str] | None, int | None]:
        """Return (proxy_dict, 1-based proxy number) in round-robin order.

        Returns ``(None, None)`` when no proxies are loaded.
        """
        with self._proxy_lock:
            if not self._proxies:
                return None, None
            num = self._proxy_index % len(self._proxies)  # 0-based position
            proxy = self._proxies[num]
            self._proxy_index += 1
        return {"http": proxy, "https": proxy}, num + 1  # 1-based

    def get_last_proxy_num(self) -> int | None:
        """Return the 1-based proxy number used for the last successful request
        on the calling thread, or ``None`` if no proxy was used.
        """
        tid = threading.get_ident()
        with self._ws_lock:
            ws = self.worker_states.get(tid)
            return ws.proxy_num if ws is not None else None

    def _blacklist_proxy(self, proxy: dict[str, str]) -> None:
        """Remove a dead proxy from the pool so it is never used again."""
        url = proxy.get("https") or proxy.get("http")
        if not url:
            return
        with self._proxy_lock:
            try:
                self._proxies.remove(url)
                print(
                    f"[webnovel-scraper] Removed dead proxy: {url} ({len(self._proxies)} remaining)"
                )
            except ValueError:
                pass  # already removed by another thread

    @staticmethod
    def _is_proxy_error(exc: BaseException) -> bool:
        """Return True when *exc* (or any chained cause) looks like a proxy-transport failure.

        curl_cffi sometimes wraps a CurlError inside a ValueError when the proxy
        sends a malformed response (e.g. curl: (43) invalid response header).
        We walk the full exception chain so those wrapped errors are caught too.
        """
        node: BaseException | None = exc
        while node is not None:
            if any(token in str(node).casefold() for token in _PROXY_CURL_ERRORS):
                return True
            node = node.__context__ if node.__cause__ is None else node.__cause__
        return False

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
    # Worker state tracking
    # ------------------------------------------------------------------

    def _ws_get_or_create(self, tid: int) -> WorkerState:
        """Return (creating if needed) the WorkerState for *tid*. Lock must be held."""
        if tid not in self.worker_states:
            self._worker_counter += 1
            self.worker_states[tid] = WorkerState(worker_num=self._worker_counter)
        return self.worker_states[tid]

    def set_worker_label(self, label: str) -> None:
        """Called by the engine before fetching a chapter to label this thread."""
        tid = threading.get_ident()
        with self._ws_lock:
            ws = self._ws_get_or_create(tid)
            ws.label = label
            ws.state = "fetch"
            ws.sleep_until = 0.0

    def clear_worker(self) -> None:
        """Mark the current thread as idle (called after a fetch completes)."""
        tid = threading.get_ident()
        with self._ws_lock:
            if tid in self.worker_states:
                ws = self.worker_states[tid]
                ws.label = ""
                ws.state = "idle"
                ws.sleep_until = 0.0

    # ------------------------------------------------------------------
    # Rate control
    # ------------------------------------------------------------------

    def _get_rate_controller(self, url: str) -> RateController:
        domain = urlparse(url).netloc.casefold()
        with self._rc_lock:
            if domain not in self._rate_controllers:
                start = _PROXY_START_INTERVAL if self._proxies else None
                self._rate_controllers[domain] = RateController(
                    domain,
                    start_interval=start,
                    backoff_factor=self.backoff_factor,
                    recovery_factor=self.recovery_factor,
                )
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
            # Wire up sleep tracking so the UI can show per-thread countdowns.
            tid = threading.get_ident()

            def _on_sleep(sleep_for: float, _tid: int = tid) -> None:
                with self._ws_lock:
                    ws = self._ws_get_or_create(_tid)
                    ws.state = "sleep"
                    ws.sleep_until = time.monotonic() + sleep_for

            rc.wait(on_sleep=_on_sleep)

            # Restore "fetch" state after any sleep.
            with self._ws_lock:
                if tid in self.worker_states:
                    ws = self.worker_states[tid]
                    if ws.state == "sleep":
                        ws.state = "fetch"
                        ws.sleep_until = 0.0

            # Pass cookies via header — hrequests has a bug where cookies= param
            # wraps a dict in a RequestsCookieJar and then tries to call .value on
            # the jar object itself, raising an AttributeError.
            headers = self._cookie_header(url)

            # --- proxy retry inner loop -----------------------------------
            # Try up to _MAX_PROXY_TRIES proxies for transport errors.
            # These failures must NOT affect the rate-limit state.
            response = None
            _used_proxy_num: int | None = None
            _proxy_tries = max(1, _MAX_PROXY_TRIES) if self._proxies else 1
            for _ in range(_proxy_tries):
                proxy, proxy_num = self._next_proxy()
                try:
                    if method.upper() == "POST":
                        response = cffi_requests.post(
                            url,
                            data=data or {},
                            headers=headers,
                            timeout=self.timeout_seconds,
                            allow_redirects=True,
                            impersonate="chrome120",
                            proxies=proxy,
                        )
                    else:
                        response = cffi_requests.get(
                            url,
                            headers=headers,
                            timeout=self.timeout_seconds,
                            allow_redirects=True,
                            impersonate="chrome120",
                            proxies=proxy,
                        )
                    _used_proxy_num = proxy_num
                    break  # transport succeeded — exit proxy-retry loop
                except Exception as exc:
                    if proxy is not None and self._is_proxy_error(exc):
                        self._blacklist_proxy(proxy)
                        continue  # try the next proxy
                    raise  # not a proxy issue — propagate normally
            # Store the winning proxy number in WorkerState for callers to read.
            with self._ws_lock:
                ws = self._ws_get_or_create(tid)
                ws.proxy_num = _used_proxy_num
            # --------------------------------------------------------------

            if response is None:
                # Every proxy attempt failed — treat as a transient error.
                last_status, last_text = 503, "All proxies failed"
                continue

            last_status = int(response.status_code)
            last_text = response.text

            if last_status == 429:
                rc.throttled()
                continue

            if self._is_bot_challenge(last_status, last_text):
                if attempt == 2:
                    # Third attempt — open visible browser for user to solve.
                    rc.throttled()
                    with self._browser_sem:
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
                    # For POST, loop again — now carrying fresh cookies.
                    continue
                # Attempts 0 and 1: throttle and retry without a browser.
                rc.throttled()
                continue

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
        response = None
        _proxy_tries = max(1, _MAX_PROXY_TRIES) if self._proxies else 1
        for _ in range(_proxy_tries):
            proxy, _pn = self._next_proxy()
            try:
                response = cffi_requests.get(
                    url,
                    headers=self._cookie_header(url),
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                    impersonate="chrome120",
                    proxies=proxy,
                )
                break
            except Exception as exc:
                if proxy is not None and self._is_proxy_error(exc):
                    self._blacklist_proxy(proxy)
                    continue
                raise
        if response is None:
            return None
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
        """Return True when an HTTP response is a Cloudflare / bot-detection wall.

        We look for specific challenge-page markers only — NOT just the string
        "cloudflare", which appears in analytics scripts on normal pages too.
        """
        if status_code == 403:
            return True
        lowered = text.casefold()
        challenge_tokens = (
            "cf-chl",  # challenge-specific class prefix
            "cf-spinner",  # Cloudflare spinner shown during JS challenge
            "cf-wrapper",  # outer wrapper on challenge pages
            "just a moment",  # Cloudflare interstitial title text
            "attention required",  # Cloudflare block page title
            "captcha",
            "error 1015",  # Cloudflare rate-limit ban page
            "you are being rate limited",  # body text on the 1015 page
        )
        return any(token in lowered for token in challenge_tokens)

    @staticmethod
    def _is_cf_challenge_in_browser(text: str) -> bool:
        """Stricter check used only when Playwright has loaded the page.

        ``_is_bot_challenge`` uses loose phrases like "just a moment" that can
        appear in ordinary chapter text and would cause the polling loop to hang
        for the full 120-second timeout on a normal page.  This method only
        matches structural markers that exist exclusively on Cloudflare challenge
        pages and nowhere else.
        """
        lowered = text.casefold()
        # <title> on a CF challenge is always exactly "just a moment..." — the
        # ellipsis is the key, it won’t appear in a novel chapter title tag.
        if "<title>just a moment" in lowered:
            return True
        # class / id names that Cloudflare injects only on challenge pages.
        strict_tokens = (
            "cf-browser-verification",
            "cf_chl_opt",  # JS variable set only on challenge pages
            "cf-spinner",
            "cf-wrapper",
            "chal-container",
            "you are being rate limited",  # 1015 body — exact phrase, safe
        )
        return any(token in lowered for token in strict_tokens)

    def _solve_challenge_in_browser(self, url: str) -> dict[str, Any]:
        """Open a visible browser so the user can pass a bot/Cloudflare challenge.

        Checks immediately after page load whether a challenge is present.  If
        the browser already shows real content (no challenge), it exits without
        user interaction.  If a challenge IS present, it polls every 1.5 s until
        it clears or the 2-minute timeout expires.

        ``browser.close()`` is always called via a ``finally`` block so the
        window never lingers regardless of exceptions.
        """
        print(
            f"\n[webnovel-scraper] Bot challenge detected for {urlparse(url).netloc}.\n"
            "  A browser window will open — if a challenge appears please wait for\n"
            "  it to clear automatically (usually 3-10 seconds).\n"
        )
        timeout_ms = int(self.timeout_seconds * 1000)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context(user_agent=user_agent)
                page = context.new_page()

                status_code = 200
                try:
                    response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    if response is not None:
                        status_code = int(response.status)
                except PlaywrightTimeoutError:
                    status_code = 408

                # Check immediately — if the browser landed on a normal page
                # (no challenge) we can take the content and leave without any
                # polling delay.
                initial_text = page.content()
                if not self._is_cf_challenge_in_browser(initial_text):
                    text = initial_text
                    raw_cookies = context.cookies()
                    cookies: dict[str, str] = {c["name"]: c["value"] for c in raw_cookies}
                    print(
                        "[webnovel-scraper] No challenge detected — using page content directly.\n"
                    )
                    return {"status_code": status_code, "text": text, "cookies": cookies}

                # Challenge IS present — poll until it clears or we time out.
                elapsed_ms = 0
                while elapsed_ms < _CHALLENGE_TIMEOUT_S * 1000:
                    page.wait_for_timeout(_CHALLENGE_POLL_INTERVAL_MS)
                    elapsed_ms += _CHALLENGE_POLL_INTERVAL_MS
                    current_text = page.content()
                    if not self._is_cf_challenge_in_browser(current_text):
                        status_code = 200
                        break

                page.wait_for_timeout(int(self.page_load_delay * 1000))
                text = page.content()
                raw_cookies = context.cookies()
                cookies = {c["name"]: c["value"] for c in raw_cookies}
            finally:
                browser.close()

        print("[webnovel-scraper] Challenge cleared. Cookies saved for future requests.\n")
        return {"status_code": status_code, "text": text, "cookies": cookies}
