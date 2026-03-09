from __future__ import annotations

from dataclasses import dataclass

from app.models import Book


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    book: Book
    score: float


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    query: str
    candidates: list[SearchCandidate]

    @property
    def accepted(self) -> list[SearchCandidate]:
        return [candidate for candidate in self.candidates if candidate.score > 85.0]


@dataclass(frozen=True, slots=True)
class DebugInfo:
    """Diagnostic snapshot of a URL — produced by ScraperEngine.debug_url()."""

    url: str
    scraper_name: str | None
    status_code: int
    used_browser_fallback: bool
    title: str | None
    chapter_count: int
    next_page_url: str | None
    raw_html_snippet: str
