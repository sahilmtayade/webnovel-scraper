from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Book, Chapter


class BaseScraper(ABC):
    site_name: str
    domains: tuple[str, ...]

    @abstractmethod
    def search(self, query: str) -> list[Book]:
        """Search books by title for this source site."""

    @abstractmethod
    def fetch_book(self, url: str) -> Book:
        """Fetch book metadata and a stub chapter list (no chapter content).

        Each returned Chapter has ``content_html=None``.  Chapter content is
        fetched separately by :meth:`fetch_chapter`, which ScraperEngine calls
        concurrently.
        """

    @abstractmethod
    def fetch_chapter(self, url: str, index: int) -> Chapter:
        """Fetch a single chapter and return it with ``content_html`` populated."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this scraper can process the given URL."""
