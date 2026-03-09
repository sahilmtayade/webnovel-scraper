from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.engine.client import NetworkClient
from app.models import Book, Chapter
from app.scrapers.base import BaseScraper

_BASE = "https://freewebnovel.com"


class FreeWebNovelScraper(BaseScraper):
    """Scraper for freewebnovel.com.

    URL patterns
    ------------
    Book page :   https://freewebnovel.com/novel/{novel-slug}
    Chapter page: https://freewebnovel.com/novel/{novel-slug}/chapter-{N}
    Search :      POST https://freewebnovel.com/search  body: searchkey=<query>
    """

    site_name = "freewebnovel"
    domains = ("freewebnovel.com", "www.freewebnovel.com")

    def __init__(self, client: NetworkClient) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        netloc = urlparse(url).netloc.casefold()
        return any(domain in netloc for domain in self.domains)

    def search(self, query: str) -> list[Book]:
        """POST search and return book stubs."""
        result = self.client.get_text(
            f"{_BASE}/search",
            method="POST",
            data={"searchkey": query},
        )
        soup = BeautifulSoup(result.text, "html.parser")

        books: list[Book] = []
        seen: set[str] = set()

        for card in soup.select(".ul-list1 .li-row .con"):
            pic_link = card.select_one(".pic a")
            if pic_link is None:
                continue
            href = pic_link.get("href")
            if not href or not isinstance(href, str):
                continue
            book_url = urljoin(_BASE, href)
            if book_url in seen:
                continue

            title_node = card.select_one(".txt h3.tit a")
            title = title_node.get_text(strip=True) if title_node else ""
            if not title:
                continue

            cover_img = pic_link.select_one("img")
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

        All chapters are listed in the page HTML under .m-newest2 — no AJAX or
        pagination required.
        """
        # Normalise to the book root (strip any trailing /chapter-N)
        book_url = re.sub(r"/chapter-\d+.*$", "", url.rstrip("/"))

        html = self.client.get_text(book_url).text
        soup = BeautifulSoup(html, "html.parser")

        title = self._book_title(soup)
        author = self._book_author(soup)
        cover_url = self._book_cover(soup)

        stubs = self._collect_stubs(soup, book_url)
        chapters = [
            Chapter(title=stub_title, url=stub_url, index=i)
            for i, (stub_title, stub_url) in enumerate(stubs, start=1)
        ]

        return Book(
            title=title,
            url=book_url,
            author=author,
            cover_url=cover_url,
            source=self.site_name,
            chapters=chapters,
        )

    def fetch_chapter(self, url: str, index: int) -> Chapter:
        """Fetch a single chapter and return it with content_html populated."""
        html = self.client.get_text(url).text
        soup = BeautifulSoup(html, "html.parser")

        # Title from OG meta — most reliable
        title = ""
        og_ch = soup.select_one("meta[property='og:novel:chapter_name']")
        if og_ch:
            raw = og_ch.get("content", "")
            if raw and isinstance(raw, str):
                title = raw.strip()
        if not title:
            h1 = soup.select_one("h1")
            title = h1.get_text(strip=True) if h1 else f"Chapter {index}"

        # Content is in div.txt (id="article")
        content_node: Tag | None = soup.select_one("div.txt#article") or soup.select_one("div.txt")
        if content_node is None:
            raise ValueError(
                f"Chapter content not found for {url} — page may be a bot-challenge or error page"
            )

        content_html = self._clean_content(content_node)

        return Chapter(title=title, url=url, index=index, content_html=content_html)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _book_title(soup: BeautifulSoup) -> str:
        node = soup.select_one("h1.tit")
        if node:
            title = node.get_text(strip=True)
            if title:
                return title
        og = soup.select_one("meta[property='og:novel:novel_name']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str):
                return raw.strip()
        return "Untitled Book"

    @staticmethod
    def _book_author(soup: BeautifulSoup) -> str | None:
        og = soup.select_one("meta[property='og:novel:author']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str):
                return raw.strip() or None
        # Fallback: author link in the info block
        author_link = soup.select_one("a[href*='/author/']")
        if author_link:
            name = author_link.get_text(strip=True)
            return name or None
        return None

    @staticmethod
    def _book_cover(soup: BeautifulSoup) -> str | None:
        img = soup.select_one(".m-imgtxt img")
        if img:
            src = img.get("src")
            if src and isinstance(src, str):
                return urljoin(_BASE, src)
        og = soup.select_one("meta[property='og:image']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str) and str(raw).startswith("http"):
                return str(raw)
        return None

    @staticmethod
    def _collect_stubs(soup: BeautifulSoup, book_url: str) -> list[tuple[str, str]]:
        """Return ordered (title, url) tuples from the chapter list.

        All chapters are inlined on the book page inside .m-newest2.
        """
        stubs: list[tuple[str, str]] = []
        seen: set[str] = set()

        for anchor in soup.select(".m-newest2 a[href*='/chapter-']"):
            href = anchor.get("href")
            if not href or not isinstance(href, str):
                continue
            chapter_url = urljoin(_BASE, href)
            if chapter_url in seen:
                continue
            title = anchor.get_text(" ", strip=True)
            seen.add(chapter_url)
            stubs.append((title or chapter_url, chapter_url))

        return stubs

    @staticmethod
    def _clean_content(node: Tag) -> str:
        """Strip scripts, ads and filler paragraphs from the content node."""
        for script in node.find_all("script"):
            script.decompose()
        for ad in node.find_all("div", id=re.compile(r"^pf-")):
            ad.decompose()
        for p in node.find_all("p"):
            text = p.get_text(strip=True).lower()
            if text.startswith("visit and read more novel") or text.startswith(
                "read latest chapters at"
            ):
                p.decompose()
        return str(node)
