from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.engine.client import NetworkClient
from app.models import Book, Chapter
from app.scrapers.base import BaseScraper

_BASE = "https://novellive.app"


class NovelliveScraper(BaseScraper):
    """Scraper for novellive.app.

    URL patterns
    ------------
    Book page :   https://novellive.app/book/{novel-slug}
    Chapter page: https://novellive.app/book/{novel-slug}/{chapter-slug}
    Search :      POST https://novellive.app/search/  body: searchkey=<query>
    """

    site_name = "novellive"
    domains = ("novellive.app", "www.novellive.app")

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
            f"{_BASE}/search/",
            method="POST",
            data={"searchkey": query},
        )
        soup = BeautifulSoup(result.text, "html.parser")

        books: list[Book] = []
        seen: set[str] = set()

        # Each result is a .li-row > .li > .con block.
        for card in soup.select(".ul-list1 .li-row .con"):
            # URL + cover from the .pic <a>
            pic_link = card.select_one(".pic a")
            if pic_link is None:
                continue
            href = pic_link.get("href")
            if not href or not isinstance(href, str):
                continue
            book_url = urljoin(_BASE, href)
            if book_url in seen:
                continue

            # Title from .txt h3.tit a
            title_node = card.select_one(".txt h3.tit a")
            title = title_node.get_text(strip=True) if title_node else ""
            if not title:
                continue

            # Cover image
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
        """Return book metadata + stub chapter list (no chapter content yet)."""
        # Strip any trailing page number so we always start from page 1.
        base_book_url = re.sub(r"/\d+$", "", url.rstrip("/"))

        first_html = self.client.get_text(base_book_url).text
        first_soup = BeautifulSoup(first_html, "html.parser")

        title = self._book_title(first_soup)
        author = self._book_author(first_soup)
        cover_url = self._book_cover(first_soup)
        total_pages = self._total_chapter_pages(first_soup)

        # Collect stubs from every paginated chapter-list page.
        stubs: list[tuple[str, str]] = []
        seen: set[str] = set()
        self._collect_stubs(first_soup, base_book_url, stubs, seen)

        for page_n in range(2, total_pages + 1):
            page_html = self.client.get_text(f"{base_book_url}/{page_n}").text
            page_soup = BeautifulSoup(page_html, "html.parser")
            self._collect_stubs(page_soup, base_book_url, stubs, seen)

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
        """Fetch a single chapter and return it with cleaned HTML content."""
        html = self.client.get_text(url).text
        soup = BeautifulSoup(html, "html.parser")

        # Title lives in <span class="chapter"> on this site.
        title_node = soup.select_one("span.chapter")
        title = title_node.get_text(strip=True) if title_node else f"Chapter {index}"

        # Content is inside div.m-read > div.txt
        content_node = soup.select_one("div.m-read div.txt")
        if content_node is None:
            # Unlikely fallback
            content_node = soup.select_one("div.txt") or soup.select_one("div.content")

        if content_node is not None:
            content_html = self._clean_content(content_node)
        else:
            content_html = "<p>(chapter content unavailable)</p>"

        return Chapter(title=title, url=url, index=index, content_html=content_html)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _book_title(soup: BeautifulSoup) -> str:
        """Extract book title from the landing page."""
        # <h1 class="tit"> inside .m-desc is the canonical book title.
        node = soup.select_one("div.m-desc h1.tit")
        if node:
            title = node.get_text(strip=True)
            if title:
                return title
        # OG meta fallback
        og = soup.select_one("meta[property='og:novel:novel_name']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str):
                return raw.strip()
        return "Untitled Book"

    @staticmethod
    def _book_author(soup: BeautifulSoup) -> str | None:
        """Extract author from the OG meta tag (most reliable location)."""
        og = soup.select_one("meta[property='og:novel:author']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str):
                return raw.strip() or None
        # Fallback: author link inside .m-imgtxt
        author_link = soup.select_one("div.m-imgtxt .txt .item .right a.a1")
        if author_link:
            name = author_link.get_text(strip=True)
            return name or None
        return None

    @staticmethod
    def _book_cover(soup: BeautifulSoup) -> str | None:
        """Extract the cover image URL."""
        # Cover image is in .m-imgtxt .pic img
        img = soup.select_one("div.m-imgtxt .pic img")
        if img:
            src = img.get("src")
            if src and isinstance(src, str) and src.startswith("http"):
                return src
        # OG image fallback
        og = soup.select_one("meta[property='og:image']")
        if og:
            raw = og.get("content", "")
            if raw and isinstance(raw, str) and str(raw).startswith("http"):
                return str(raw)
        return None

    @staticmethod
    def _total_chapter_pages(soup: BeautifulSoup) -> int:
        """Return the highest page number from the chapter-list pagination select."""
        select = soup.select_one("div.m-newest2 select#indexselect")
        if select is None:
            return 1
        options = select.select("option")
        if not options:
            return 1
        last_option = options[-1]
        value = last_option.get("value", "1")
        try:
            return int(str(value))
        except ValueError:
            return 1

    @staticmethod
    def _collect_stubs(
        soup: BeautifulSoup,
        base_book_url: str,
        stubs: list[tuple[str, str]],
        seen: set[str],
    ) -> None:
        """Append (title, url) stubs from the chapter list on this page."""
        # Chapter list lives in: div.m-newest2 > ul.ul-list5 > li > a.con
        for anchor in soup.select("div.m-newest2 ul.ul-list5 li a.con[href]"):
            href = anchor.get("href")
            if not href or not isinstance(href, str):
                continue
            chapter_url = urljoin(base_book_url, href)
            if chapter_url in seen:
                continue
            # Prefer the title attribute; fall back to link text.
            title_attr = anchor.get("title")
            title = (
                str(title_attr).strip()
                if title_attr and isinstance(title_attr, str)
                else anchor.get_text(" ", strip=True)
            )
            seen.add(chapter_url)
            stubs.append((title or chapter_url, chapter_url))

    @staticmethod
    def _clean_content(node: Tag) -> str:
        """Strip ad blocks and scripts from a content div, return inner HTML."""
        # Remove all <script> tags
        for script in node.find_all("script"):
            script.decompose()
        # Remove ad divs (id starts with "pf-")
        for ad in node.find_all("div", id=re.compile(r"^pf-")):
            ad.decompose()
        # Remove the "Visit and read more novel…" trailing filler paragraph
        for p in node.find_all("p"):
            text = p.get_text(strip=True).lower()
            if text.startswith("visit and read more novel"):
                p.decompose()
        return str(node)
