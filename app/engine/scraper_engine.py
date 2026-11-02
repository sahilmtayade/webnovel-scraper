from __future__ import annotations

import math
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.engine.client import NetworkClient, ProxyMode
from app.engine.content import EmptyChapterError, has_body
from app.engine.rate import _DEFAULT_BACKOFF_FACTOR, _DEFAULT_RECOVERY_FACTOR
from app.engine.types import DebugInfo, DownloadTick, SearchCandidate, SearchOutcome
from app.models import Book, Chapter
from app.scrapers.base import BaseScraper
from app.scrapers.freewebnovel import FreeWebNovelScraper
from app.scrapers.novelfull import NovelFullScraper

# Chapters are retried up to this many times total (1 initial + N-1 retries).
_MAX_CHAPTER_ATTEMPTS = 4
# Seconds to wait before each retry round (indexed by retry number, 0-based).
_RETRY_BACKOFF_S: list[float] = [3.0, 8.0, 20.0]
# Initial estimate of per-chapter fetch time used before real data is available.
_EST_FETCH_TIME_S: float = 1.5
# The final attempt escalates from the HTTP client to a Playwright browser.
_BROWSER_ATTEMPT = _MAX_CHAPTER_ATTEMPTS
# Body-less results in a row, on a non-proxied (direct) connection, before every
# fetch is switched to the browser. The direct connection is what gets flagged,
# so this is tracked per connection rather than per chapter.
_DIRECT_FAIL_THRESHOLD = 3
# While in browser mode, every Nth fetch is a plain-HTTP probe to see if the
# direct connection has recovered.
_BROWSER_PROBE_EVERY = 25
# Exponential moving average weight for fetch-time updates (higher = faster adapt).
_EMA_ALPHA: float = 0.2


class ScraperEngine:
    def __init__(
        self,
        scrapers: list[BaseScraper],
        client: NetworkClient | None = None,
        max_workers: int = 6,
    ) -> None:
        self.scrapers = scrapers
        self._client = client or NetworkClient()
        self._max_workers = max_workers
        # Rolling average of observed chapter fetch time (updated via EMA).
        # Used to decide how many threads are actually useful.
        self._avg_fetch_time_s: float = _EST_FETCH_TIME_S
        # Connection-level escalation state (see _fetch_validated).
        self._mode_lock = threading.Lock()
        self._direct_fail_streak = 0
        self._browser_mode = False
        self._browser_fetches = 0
        self._on_status: Callable[[str], None] | None = None

    @classmethod
    def with_defaults(
        cls,
        page_load_delay: float = 1.0,
        max_workers: int = 8,
        max_browser_sessions: int = 3,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        recovery_factor: float = _DEFAULT_RECOVERY_FACTOR,
        proxy_mode: ProxyMode = "auto",
    ) -> ScraperEngine:
        client = NetworkClient(
            page_load_delay=page_load_delay,
            max_browser_sessions=max_browser_sessions,
            backoff_factor=backoff_factor,
            recovery_factor=recovery_factor,
            proxy_mode=proxy_mode,
        )
        return cls(
            scrapers=[
                FreeWebNovelScraper(client=client),
                NovelFullScraper(client=client),
                # NovelliveScraper(client=client), Cloudflare blocks
            ],
            client=client,
            max_workers=max_workers,
        )

    def _fetch_validated(
        self, scraper: BaseScraper, ch: Chapter, force_browser: bool = False
    ) -> Chapter:
        """Fetch a chapter; a body-less result counts as a failure.

        Escalation is tied to the connection, not the chapter: when requests go
        out directly (proxy off, or fallback not using a proxy) and several come
        back body-less in a row, the direct IP is likely flagged, so *all*
        fetches switch to Playwright until a periodic plain probe succeeds.
        Proxied requests rotate, so their failures do not trigger this.
        """
        with self._mode_lock:
            use_browser = force_browser
            if self._browser_mode:
                self._browser_fetches += 1
                if self._browser_fetches % _BROWSER_PROBE_EVERY != 0:
                    use_browser = True

        try:
            result = scraper.fetch_chapter(ch.url, ch.index, use_browser=use_browser)
            ok = has_body(result.content_html)
        except Exception:
            self._client.record_outcome(False)
            self._record_direct(use_browser, ok=False)
            raise
        self._client.record_outcome(ok)
        self._record_direct(use_browser, ok=ok)
        if not ok:
            raise EmptyChapterError(f"Chapter has no body text ({ch.title})")
        return result

    def _record_direct(self, used_browser: bool, ok: bool) -> None:
        if used_browser or self._client.get_last_proxy_num() is not None:
            return  # browser fetches and proxied requests say nothing about the direct IP
        with self._mode_lock:
            if ok:
                self._direct_fail_streak = 0
                if self._browser_mode:
                    self._browser_mode = False
                    self._notify("Direct connection recovered — back to plain HTTP.")
                return
            self._direct_fail_streak += 1
            if self._direct_fail_streak >= _DIRECT_FAIL_THRESHOLD and not self._browser_mode:
                self._browser_mode = True
                self._browser_fetches = 0
                self._notify(
                    f"{_DIRECT_FAIL_THRESHOLD} body-less chapters in a row on the direct "
                    "connection — switching to browser fetching."
                )

    def _notify(self, msg: str) -> None:
        if self._on_status is not None:
            self._on_status(msg)

    def _optimal_workers(self, url: str) -> int:
        """Compute the optimal thread count for *url*'s domain.

        Multiple threads only help when the per-request HTTP round-trip time
        exceeds the rate-controller interval.  If the server has slowed us
        down to e.g. one request every 3 s, and each fetch takes ~1.5 s,
        a single thread is perfectly sufficient — it finishes before the next
        slot opens anyway.

        Formula: workers = ceil(avg_fetch_time / rate_interval), capped at
        self._max_workers.
        """
        rc = self._client._get_rate_controller(url)
        interval = rc.current_interval
        needed = math.ceil(self._avg_fetch_time_s / max(interval, 1e-9))
        return max(1, min(self._max_workers, needed))

    def search(self, query: str) -> SearchOutcome:
        candidates: list[SearchCandidate] = []

        for scraper in self.scrapers:
            try:
                for book in scraper.search(query):
                    score = float(fuzz.WRatio(query.casefold(), book.title.casefold()))
                    candidates.append(SearchCandidate(book=book, score=score))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[webnovel-scraper] Scraper {scraper.site_name!r} failed during search: {exc}\n{traceback.format_exc()}"
                )
                continue

        ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
        return SearchOutcome(query=query, candidates=ordered)

    def fetch_book_meta(self, url: str) -> Book:
        """Fetch book metadata and stub chapter list — no chapter content."""
        scraper = self._resolve_scraper(url)
        if scraper is None:
            raise ValueError(f"No scraper registered for URL: {url}")
        # A 200 response can still be a challenge/error page with no chapter list;
        # retry a few times rather than silently returning an empty book.
        for attempt in range(3):
            book = scraper.fetch_book(url)
            if book.chapters:
                return book
            time.sleep(2.0 * (attempt + 1))
        raise ValueError(
            f"No chapters found at {url} — the site may have served a challenge or error page"
        )

    def download_chapters(
        self,
        book: Book,
        on_tick: Callable[[DownloadTick], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        start_chapter: int | None = None,
        end_chapter: int | None = None,
    ) -> Book:
        """Concurrently fetch all chapter content and return a completed Book.

        Parameters
        ----------
        start_chapter, end_chapter:
            1-based chapter numbers (inclusive on both ends).  When provided,
            only chapters in that range are downloaded; the rest are kept as
            content-free stubs so the Book model stays valid.
        """
        scraper = self._resolve_scraper(book.url)
        if scraper is None:
            raise ValueError(f"No scraper registered for URL: {book.url}")

        all_chapters = book.chapters
        # Apply range filter
        if start_chapter is not None or end_chapter is not None:
            lo = (start_chapter - 1) if start_chapter is not None else 0
            hi = end_chapter if end_chapter is not None else len(all_chapters)
            chapters_to_fetch = all_chapters[lo:hi]
        else:
            chapters_to_fetch = list(all_chapters)
        # Resume: chapters that already carry real body text (e.g. loaded from an
        # existing EPUB) are kept as-is; only missing / body-less ones are fetched.
        chapters_to_fetch = [c for c in chapters_to_fetch if not has_body(c.content_html)]

        total = len(chapters_to_fetch)
        self._on_status = on_status
        self._client.interrupted.clear()
        self._direct_fail_streak = 0
        self._browser_mode = False
        succeeded = 0
        failed = 0
        rate_limited = 0
        interrupted = False

        # Final results keyed by chapter index; filled in as chapters resolve.
        results: dict[int, Chapter] = {}
        # Proxy number (1-based) used for each chapter; populated by worker threads.
        proxy_nums: dict[int, int | None] = {}
        # Chapters still needing another attempt: list of (stub, last_error).
        to_retry: list[tuple[Chapter, str]] = []

        def _emit(tick: DownloadTick) -> None:
            if on_tick is not None:
                on_tick(tick)

        # ── Phase 1: initial concurrent pass ──────────────────────────────────
        # Condition used to serialise rate-slot acquisition: only
        # _optimal_workers() threads may call rc.wait() concurrently.  When
        # the server throttles us the optimal count drops (often to 1), so
        # excess threads block here instead of pre-booking future rate slots.
        _inflight_cond = threading.Condition()
        _inflight_count = [0]

        def _acquire_slot() -> None:
            with _inflight_cond:
                while _inflight_count[0] >= max(1, self._optimal_workers(book.url)):
                    _inflight_cond.wait(timeout=0.5)
                _inflight_count[0] += 1

        def _release_slot() -> None:
            with _inflight_cond:
                _inflight_count[0] -= 1
                _inflight_cond.notify_all()

        def _fetch(ch: Chapter) -> Chapter:
            """Wait for an in-flight slot then fetch, keeping worker state updated."""
            _acquire_slot()
            try:
                self._client._check_interrupted()
                t0 = time.monotonic()
                self._client.set_worker_label(ch.title)
                try:
                    result = self._fetch_validated(scraper, ch)
                finally:
                    self._client.clear_worker()
                # Capture proxy_num here, while still on the worker thread.
                proxy_nums[ch.index] = self._client.get_last_proxy_num()
                elapsed = time.monotonic() - t0
                # EMA update of average fetch time (used by _optimal_workers).
                self._avg_fetch_time_s = (
                    _EMA_ALPHA * elapsed + (1.0 - _EMA_ALPHA) * self._avg_fetch_time_s
                )
                return result
            finally:
                _release_slot()

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {executor.submit(_fetch, ch): ch for ch in chapters_to_fetch}
            try:
                for future in as_completed(futures):
                    stub = futures[future]
                    try:
                        result = future.result()
                        results[stub.index] = result
                        succeeded += 1
                        _emit(
                            DownloadTick(
                                total=total,
                                succeeded=succeeded,
                                failed=failed,
                                rate_limited=rate_limited,
                                chapter_title=result.title,
                                chapter_index=stub.index,
                                error=None,
                                attempt=1,
                                max_attempts=_MAX_CHAPTER_ATTEMPTS,
                                proxy_num=proxy_nums.get(stub.index),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        to_retry.append((stub, str(exc)))
                        _emit(
                            DownloadTick(
                                total=total,
                                succeeded=succeeded,
                                failed=failed,
                                rate_limited=rate_limited,
                                chapter_title=stub.title,
                                chapter_index=stub.index,
                                error=str(exc),
                                attempt=1,
                                max_attempts=_MAX_CHAPTER_ATTEMPTS,
                            )
                        )
            except KeyboardInterrupt:
                interrupted = True
                self._client.interrupted.set()
                if on_status is not None:
                    on_status(
                        "Interrupting — letting in-flight requests finish, then saving what we have…"
                    )
                for future in futures:
                    if not future.done():
                        future.cancel()

        if interrupted:
            if on_status is not None:
                on_status("Finalizing partial book after interruption...")
                on_status("")

            fetched = {ch.index: ch for ch in results.values()}
            merged = [fetched.get(ch.index, ch) for ch in all_chapters]
            return book.model_copy(update={"chapters": merged})

        # ── Phase 2: sequential retries with backoff ───────────────────────────
        for attempt_n in range(2, _MAX_CHAPTER_ATTEMPTS + 1):
            if not to_retry or interrupted:
                break
            # Recompute after each backoff: interval may have changed.
            dynamic_workers = self._optimal_workers(book.url)
            backoff = _RETRY_BACKOFF_S[attempt_n - 2]
            is_last = attempt_n == _MAX_CHAPTER_ATTEMPTS
            if on_status is not None:
                mode = " via browser" if attempt_n >= _BROWSER_ATTEMPT else ""
                on_status(
                    f"Retrying {len(to_retry)} chapter(s){mode} "
                    f"(attempt {attempt_n}/{_MAX_CHAPTER_ATTEMPTS}, "
                    f"waiting {backoff:.0f}s…)"
                )
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                interrupted = True
                self._client.interrupted.set()
                if on_status is not None:
                    on_status(
                        "Interrupting — letting in-flight requests finish, then saving what we have…"
                    )
                break

            still_failing: list[tuple[Chapter, str]] = []
            for stub, _ in to_retry:
                if interrupted:
                    break
                try:
                    self._client.set_worker_label(stub.title)
                    try:
                        result = self._fetch_validated(
                            scraper, stub, force_browser=attempt_n >= _BROWSER_ATTEMPT
                        )
                    finally:
                        self._client.clear_worker()
                    # Capture proxy_num here, while still on the (sequential retry) thread.
                    proxy_nums[stub.index] = self._client.get_last_proxy_num()
                    results[stub.index] = result
                    succeeded += 1
                    _emit(
                        DownloadTick(
                            total=total,
                            succeeded=succeeded,
                            failed=failed,
                            rate_limited=rate_limited,
                            chapter_title=result.title,
                            chapter_index=stub.index,
                            error=None,
                            attempt=attempt_n,
                            max_attempts=_MAX_CHAPTER_ATTEMPTS,
                            active_workers=dynamic_workers,
                            proxy_num=proxy_nums.get(stub.index),
                        )
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    self._client.interrupted.set()
                    if on_status is not None:
                        on_status(
                            "Interrupting — letting in-flight requests finish, then saving what we have…"
                        )
                    break
                except Exception as exc:  # noqa: BLE001
                    error_msg = str(exc)
                    if is_last:
                        # All retries exhausted — record permanent failure.
                        failed += 1
                        if "429" in error_msg or "rate" in error_msg.lower():
                            rate_limited += 1
                        # Keep any previously saved content (resume); otherwise a placeholder.
                        results[stub.index] = (
                            stub
                            if stub.content_html is not None
                            else stub.model_copy(
                                update={"content_html": f"<p>[Download failed: {error_msg}]</p>"}
                            )
                        )
                    else:
                        still_failing.append((stub, error_msg))
                    _emit(
                        DownloadTick(
                            total=total,
                            succeeded=succeeded,
                            failed=failed,
                            rate_limited=rate_limited,
                            chapter_title=stub.title,
                            chapter_index=stub.index,
                            error=error_msg,
                            attempt=attempt_n,
                            max_attempts=_MAX_CHAPTER_ATTEMPTS,
                            active_workers=dynamic_workers,
                        )
                    )
            if interrupted:
                break
            to_retry = still_failing

        if on_status is not None:
            on_status("")

        # Merge fetched results back onto all chapters (unfetched stay as stubs)
        fetched = {ch.index: ch for ch in results.values()}
        merged = [
            fetched.get(ch.index, ch)  # use fetched version if available, else stub
            for ch in all_chapters
        ]
        return book.model_copy(update={"chapters": merged})

    def download(self, url: str) -> Book:
        """Convenience wrapper: fetch metadata then all chapters sequentially."""
        return self.download_chapters(self.fetch_book_meta(url))

    def _resolve_scraper(self, url: str) -> BaseScraper | None:
        for scraper in self.scrapers:
            if scraper.can_handle(url):
                return scraper
        return None

    def debug_url(self, url: str) -> DebugInfo:
        """Fetch a URL and return diagnostic information without downloading chapters."""
        scraper = self._resolve_scraper(url)
        result = self._client.get_text(url)
        soup = BeautifulSoup(result.text, "html.parser")

        # Generic title extraction
        title: str | None = None
        h1 = soup.find("h1")
        if h1 is not None:
            title = h1.get_text(strip=True) or None
        if title is None:
            og = soup.find("meta", property="og:title")
            if og is not None:
                raw = og.get("content")
                if isinstance(raw, str):
                    title = raw.strip() or None

        # Count chapter-like links visible on the landing page
        chapter_links = [
            a
            for a in soup.select("a[href]")
            if isinstance(a.get("href"), str) and "chapter" in str(a.get("href")).casefold()
        ]

        # Generic pagination detection: look for <link rel="next">
        next_page: str | None = None
        rel_next = soup.find("link", rel="next")
        if rel_next is not None:
            href = rel_next.get("href")
            if isinstance(href, str) and href:
                next_page = href

        return DebugInfo(
            url=url,
            scraper_name=scraper.site_name if scraper is not None else None,
            status_code=result.status_code,
            used_browser_fallback=result.used_browser_fallback,
            title=title,
            chapter_count=len(chapter_links),
            next_page_url=next_page,
            raw_html_snippet=result.text[:800],
        )
