from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.engine.client import NetworkClient
from app.engine.types import DebugInfo, SearchCandidate, SearchOutcome
from app.models import Book, Chapter
from app.scrapers.base import BaseScraper
from app.scrapers.novellive import NovelliveScraper


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

    @classmethod
    def with_defaults(
        cls,
        page_load_delay: float = 1.0,
        max_workers: int = 6,
    ) -> ScraperEngine:
        client = NetworkClient(page_load_delay=page_load_delay)
        return cls(
            scrapers=[NovelliveScraper(client=client)], client=client, max_workers=max_workers
        )

    def search(self, query: str) -> SearchOutcome:
        candidates: list[SearchCandidate] = []

        for scraper in self.scrapers:
            for book in scraper.search(query):
                score = float(fuzz.WRatio(query.casefold(), book.title.casefold()))
                candidates.append(SearchCandidate(book=book, score=score))

        ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
        return SearchOutcome(query=query, candidates=ordered)

    def fetch_book_meta(self, url: str) -> Book:
        """Fetch book metadata and stub chapter list — no chapter content."""
        scraper = self._resolve_scraper(url)
        if scraper is None:
            raise ValueError(f"No scraper registered for URL: {url}")
        return scraper.fetch_book(url)

    def download_chapters(
        self,
        book: Book,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Book:
        """Concurrently fetch all chapter content and return a completed Book.

        *on_progress* is called as ``on_progress(completed, total)`` after each
        chapter finishes.  The first call always has ``completed=0`` so callers
        can initialise a progress bar with the known total.
        """
        scraper = self._resolve_scraper(book.url)
        if scraper is None:
            raise ValueError(f"No scraper registered for URL: {book.url}")

        total = len(book.chapters)
        if on_progress is not None:
            on_progress(0, total)

        chapters: list[Chapter] = []
        completed = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(scraper.fetch_chapter, ch.url, ch.index): ch.index
                for ch in book.chapters
            }
            for future in as_completed(futures):
                chapters.append(future.result())
                completed += 1
                if on_progress is not None:
                    on_progress(completed, total)

        chapters.sort(key=lambda c: c.index)
        return book.model_copy(update={"chapters": chapters})

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
