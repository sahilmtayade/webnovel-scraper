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


@dataclass(slots=True)
class DownloadTick:
    """Emitted after every chapter attempt during a concurrent download."""

    total: int
    succeeded: int
    failed: int  # permanently failed — all retries exhausted
    rate_limited: int  # subset of failed: rate-limit (429) errors
    chapter_title: str
    chapter_index: int
    error: str | None  # None = success
    attempt: int = 1  # 1-based attempt number
    max_attempts: int = 1  # total attempts allowed (including first try)
    active_workers: int = 1  # worker threads in use for this download phase
