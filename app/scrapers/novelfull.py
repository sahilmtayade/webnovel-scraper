from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from app.engine.client import NetworkClient
from app.models import Book, Chapter
from app.scrapers.base import BaseScraper

_BASE = "https://novelfull.com"


class NovelFullScraper(BaseScraper):
    """Scraper for novelfull.com.

    URL patterns
    ------------
    Book page :   https://novelfull.com/{novel-slug}.html
    Book page 2+: https://novelfull.com/{novel-slug}.html?page=2
    Chapter page: https://novelfull.com/{novel-slug}/{chapter-slug}.html
    Search :      GET  https://novelfull.com/search?keyword=<query>
    """

    site_name = "novelfull"
    domains = ("novelfull.com", "www.novelfull.com")

    def __init__(self, client: NetworkClient) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        netloc = urlparse(url).netloc.casefold()
        return any(domain in netloc for domain in self.domains)

    def search(self, query: str) -> list[Book]:
        """GET search and return book stubs."""
        search_url = f"{_BASE}/search?{urlencode({'keyword': query})}"
        result = self.client.get_text(search_url)
        soup = BeautifulSoup(result.text, "html.parser")

        books: list[Book] = []
        seen: set[str] = set()

        # Each result row: div.row inside #list-page .list-truyen
        for row in soup.select("#list-page .list-truyen .row"):
            title_anchor = row.select_one("h3.truyen-title a")
            if title_anchor is None:
                continue
            href = title_anchor.get("href")
            if not href or not isinstance(href, str):
                continue
            book_url = urljoin(_BASE, href)
            if book_url in seen:
                continue

            title = title_anchor.get_text(strip=True)
            if not title:
                continue

            # Cover image
            cover_img = row.select_one("div.col-xs-3 img")
            cover_src = cover_img.get("src") if cover_img else None
            cover_url = (
                urljoin(_BASE, str(cover_src)) if cover_src and isinstance(cover_src, str) else None
            )

            seen.add(book_url)
            books.append(
                Book(
                    title=title,
                    url=book_url,
                    cover_url=cover_url,
                    source=self.site_name,
                    chapters=[],
                )
            )

        return books

    def fetch_book(self, url: str) -> Book:
        """Return book metadata + full stub chapter list (no chapter content yet).

        The chapter list is paginated — each page exposes ~50 chapters.
        All pages are fetched sequentially.
        """
        base_book_url = self._normalize_book_url(url)

        html = self.client.get_text(base_book_url).text
        soup = BeautifulSoup(html, "html.parser")

        title = self._book_title(soup)
        author = self._book_author(soup)
        cover_url = self._book_cover(soup)
        total_pages = self._total_chapter_pages(soup, base_book_url)

        stubs: list[tuple[str, str]] = []
        seen: set[str] = set()
        self._collect_stubs(soup, stubs, seen)

        for page_n in range(2, total_pages + 1):
            page_url = f"{base_book_url}?page={page_n}"
            page_html = self.client.get_text(page_url).text
            page_soup = BeautifulSoup(page_html, "html.parser")
            self._collect_stubs(page_soup, stubs, seen)

        chapters = [
            Chapter(title=stub_title, url=stub_url, index=i)
            for i, (stub_title, stub_url) in enumerate(stubs, start=1)
        ]

        return Book(
            title=title,
            url=base_book_url,
            author=author,
            cover_url=cover_url,
            source=self.site_name,
            chapters=chapters,
        )

    def fetch_chapter(self, url: str, index: int) -> Chapter:
        """Fetch a single chapter and return it with content_html populated."""
        html = self.client.get_text(url).text
        soup = BeautifulSoup(html, "html.parser")

        # Title: <h2><a class="chapter-title" ...><span class="chapter-text">…</span></a></h2>
        title_node = soup.select_one("h2 span.chapter-text")
        if title_node:
            title = title_node.get_text(strip=True)
        else:
            title_node = soup.select_one("h2 a.chapter-title")
            title = title_node.get_text(strip=True) if title_node else f"Chapter {index}"

        # Content: div#chapter-content
        content_node = soup.select_one("div#chapter-content")
        if content_node is None:
            content_html = "<p>(chapter content unavailable)</p>"
        else:
            content_html = self._clean_content(content_node)

        return Chapter(title=title, url=url, index=index, content_html=content_html)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_book_url(url: str) -> str:
        """Strip query parameters from a book URL so we always start from page 1."""
        parsed = urlparse(url)
        return urlunparse(parsed._replace(query="", fragment=""))

    @staticmethod
    def _book_title(soup: BeautifulSoup) -> str:
        """Extract book title from the book landing page."""
        # <h3 class="title"> appears twice; the one inside .col-info-desc is canonical.
        node = soup.select_one("div.col-info-desc h3.title")
        if node:
            title = node.get_text(strip=True)
            if title:
                return title
        # Fallback: any h3.title
        node = soup.select_one("h3.title")
        if node:
            title = node.get_text(strip=True)
            if title:
                return title
        # Meta fallback
        meta = soup.select_one("meta[name='title']")
        if meta:
            raw = meta.get("content", "")
            if raw and isinstance(raw, str):
                # Strip site suffix like " - Novelfull"
                return re.sub(r"\s*[-–|].*$", "", raw).strip()
        return "Untitled Book"

    @staticmethod
    def _book_author(soup: BeautifulSoup) -> str | None:
        """Extract the first author from the info block."""
        author_link = soup.select_one(".info a[href*='/author/']")
        if author_link:
            name = author_link.get_text(strip=True)
            return name or None
        return None

    @staticmethod
    def _book_cover(soup: BeautifulSoup) -> str | None:
        """Extract the cover image URL."""
        img = soup.select_one(".book img")
        if img:
            src = img.get("src")
            if src and isinstance(src, str):
                return urljoin(_BASE, src)
        # OG meta fallback
        meta = soup.select_one("meta[name='image']")
        if meta:
            raw = meta.get("content", "")
            if raw and isinstance(raw, str) and str(raw).startswith("http"):
                return str(raw)
        return None

    @staticmethod
    def _total_chapter_pages(soup: BeautifulSoup, base_book_url: str) -> int:
        """Return the total number of chapter-list pages from the pagination widget."""
        # Look for the "Last »" link in the pagination bar.
        last_link = soup.select_one("ul.pagination li.last a[href]")
        if last_link:
            href = last_link.get("href")
            if href and isinstance(href, str):
                # href looks like "/martial-world.html?page=46"
                qs = parse_qs(urlparse(href).query)
                page_vals = qs.get("page")
                if page_vals:
                    try:
                        return int(page_vals[0])
                    except ValueError:
                        pass
        return 1

    @staticmethod
    def _collect_stubs(
        soup: BeautifulSoup,
        stubs: list[tuple[str, str]],
        seen: set[str],
    ) -> None:
        """Append (title, url) stubs from all chapter lists on this page."""
        for anchor in soup.select("#list-chapter ul.list-chapter li a[href]"):
            href = anchor.get("href")
            if not href or not isinstance(href, str):
                continue
            chapter_url = urljoin(_BASE, href)
            if chapter_url in seen:
                continue
            # Prefer title attribute; fall back to span.chapter-text or link text
            title_attr = anchor.get("title")
            if title_attr and isinstance(title_attr, str) and title_attr.strip():
                stub_title = title_attr.strip()
            else:
                span = anchor.select_one("span.chapter-text")
                stub_title = span.get_text(strip=True) if span else anchor.get_text(" ", strip=True)
            seen.add(chapter_url)
            stubs.append((stub_title or chapter_url, chapter_url))

    @staticmethod
    def _clean_content(node: Tag) -> str:
        """Strip ads, scripts and navigation noise from the chapter content node."""
        # Remove <script> and <iframe> tags
        for tag in node.find_all(["script", "iframe"]):
            tag.decompose()
        # Remove ad holder divs
        for div in node.find_all("div", class_=re.compile(r"\bads-holder\b")):
            div.decompose()
        # Remove divs with align attribute (typically used for ad wrappers)
        for div in node.find_all("div", attrs={"align": True}):
            div.decompose()
        # Remove empty paragraphs
        for p in node.find_all("p"):
            if not p.get_text(strip=True):
                p.decompose()
        return str(node)
