"""Minimal tests for the logic most likely to break silently."""

from __future__ import annotations

import threading
from pathlib import Path

from app.engine import scraper_engine
from app.engine.content import has_body
from app.engine.epub import EpubBuilder
from app.engine.scraper_engine import ScraperEngine
from app.models import Book, Chapter
from app.output import find_existing, merge_existing
from app.scrapers.base import BaseScraper

PROSE = "<p>" + "The hero walked on. " * 10 + "</p>"
TITLE_ONLY = "<h2>Chapter 5: Corpses</h2><p>Translator: X</p><p>Editor: Y</p>"


def test_has_body():
    assert has_body(f"<h2>Chapter 1: A</h2>{PROSE}")
    assert not has_body(TITLE_ONLY)
    assert not has_body("<p>(chapter content unavailable)</p>")
    assert not has_body("<p>[Download failed: " + "x" * 200 + "]</p>")
    assert not has_body(None)


def _book(contents: dict[int, str | None]) -> Book:
    chapters = [
        Chapter(title=f"Chapter {i}", url=f"http://x/{i}", index=i, content_html=html)
        for i, html in contents.items()
    ]
    return Book(title="T", url="http://x", source="fake", chapters=chapters)


class _NoNetwork:
    def get_binary(self, url):  # EpubBuilder cover download
        return None


def test_epub_roundtrip_and_resume(tmp_path: Path):
    book = _book({1: PROSE, 2: TITLE_ONLY, 3: None})
    path = EpubBuilder(network_client=_NoNetwork()).build(book, tmp_path)  # type: ignore[arg-type]

    # Same file is not overwritten by a second build.
    again = EpubBuilder(network_client=_NoNetwork()).build(book, tmp_path)  # type: ignore[arg-type]
    assert again != path

    [existing, *_] = [e for e in find_existing(book, tmp_path) if e.path == path]
    assert (existing.total, existing.complete) == (2, 1)

    fresh = _book({1: None, 2: None, 3: None})
    merged, preserved = merge_existing(fresh, path)
    assert preserved == {1, 2}
    assert has_body(merged.chapters[0].content_html)
    assert not has_body(merged.chapters[1].content_html)  # still needs refetch


class _FlakyScraper(BaseScraper):
    site_name = "fake"
    domains = ("x",)

    def __init__(self):
        self.calls: list[tuple[int, bool]] = []

    def search(self, query):
        return []

    def fetch_book(self, url):
        raise NotImplementedError

    def can_handle(self, url):
        return True

    def fetch_chapter(self, url, index, use_browser=False):
        self.calls.append((index, use_browser))
        # Chapter 2 only has a body when loaded through the browser.
        html = PROSE if (index != 2 or use_browser) else TITLE_ONLY
        return Chapter(title=f"Chapter {index}", url=url, index=index, content_html=html)


class _FakeClient:
    interrupted = threading.Event()

    def __getattr__(self, name):
        class _RC:
            current_interval = 0.0

        return lambda *a, **k: _RC() if name == "_get_rate_controller" else None


def test_empty_chapter_escalates_to_browser(monkeypatch):
    monkeypatch.setattr(scraper_engine, "_RETRY_BACKOFF_S", [0.0, 0.0, 0.0])
    scraper = _FlakyScraper()
    engine = ScraperEngine([scraper], client=_FakeClient(), max_workers=1)  # type: ignore[arg-type]

    # Chapter 1 already has a body (resume) and must not be refetched.
    book = _book({1: PROSE, 2: None, 3: None})
    result = engine.download_chapters(book)

    assert all(has_body(c.content_html) for c in result.chapters)
    assert 1 not in [i for i, _ in scraper.calls]
    assert (2, True) in scraper.calls and (3, True) not in scraper.calls


class _BlockedDirectScraper(_FlakyScraper):
    """Direct HTTP never returns a body; the browser always does."""

    def fetch_chapter(self, url, index, use_browser=False):
        self.calls.append((index, use_browser))
        html = PROSE if use_browser else TITLE_ONLY
        return Chapter(title=f"Chapter {index}", url=url, index=index, content_html=html)


def test_direct_failures_switch_all_fetches_to_browser(monkeypatch):
    monkeypatch.setattr(scraper_engine, "_RETRY_BACKOFF_S", [0.0, 0.0, 0.0])
    scraper = _BlockedDirectScraper()
    engine = ScraperEngine([scraper], client=_FakeClient(), max_workers=1)  # type: ignore[arg-type]

    result = engine.download_chapters(_book({i: None for i in range(1, 9)}))

    assert all(has_body(c.content_html) for c in result.chapters)
    # After 3 plain failures, the remaining first-pass fetches go straight to the browser.
    first_pass = scraper.calls[:8]
    assert [b for _, b in first_pass[:3]] == [False] * 3
    assert all(b for _, b in first_pass[3:])
