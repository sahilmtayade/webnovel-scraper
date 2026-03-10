from __future__ import annotations

import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

from app.engine.client import NetworkClient
from app.models import Book, Chapter

_REPO_URL = "https://github.com/sahilmtayade/webnovel-scraper"

_COMMON_CSS = """
@import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,700;1,400&display=swap');

body {
    margin: 0;
    padding: 0;
    background: #1a1a1a;
    color: #e8e0d0;
    font-family: 'EB Garamond', Georgia, serif;
}

/* ── cover page ─────────────────────────────────────────── */
.cover-page {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    padding: 2em;
    box-sizing: border-box;
    text-align: center;
    background: linear-gradient(160deg, #1a1a1a 0%, #2a1f1f 100%);
}
.cover-page img {
    max-width: 420px;
    width: 90%;
    border-radius: 6px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.7);
    margin-bottom: 1.6em;
}
.cover-title {
    font-size: 2em;
    font-weight: 700;
    letter-spacing: 0.03em;
    margin: 0 0 0.3em;
    color: #f0e6cc;
}
.cover-author {
    font-size: 1.1em;
    font-style: italic;
    color: #b0a090;
    margin: 0;
}

/* ── info / credits page ────────────────────────────────── */
.info-page {
    max-width: 640px;
    margin: 0 auto;
    padding: 3em 2em 4em;
}
.info-page h1 {
    font-size: 1.5em;
    font-weight: 700;
    border-bottom: 1px solid #444;
    padding-bottom: 0.4em;
    margin-bottom: 1.2em;
    color: #f0e6cc;
}
.info-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.95em;
}
.info-table td {
    padding: 0.5em 0.6em;
    vertical-align: top;
    border-bottom: 1px solid #2e2e2e;
}
.info-table td:first-child {
    white-space: nowrap;
    color: #9e8e78;
    font-weight: 700;
    padding-right: 1.2em;
    width: 1%;
}
.info-page a {
    color: #c09060;
    text-decoration: none;
}
.info-page a:hover {
    text-decoration: underline;
}
.info-page .credits {
    margin-top: 2.5em;
    font-size: 0.85em;
    color: #666;
    border-top: 1px solid #2e2e2e;
    padding-top: 1em;
}
"""


class EpubBuilder:
    def __init__(self, network_client: NetworkClient | None = None) -> None:
        self.network_client = network_client or NetworkClient()

    def build(self, book: Book, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)

        included_chapters = [c for c in book.chapters if c.content_html is not None]
        if included_chapters:
            first_idx = included_chapters[0].index
            last_idx = included_chapters[-1].index
            chapter_suffix = (
                f" [Ch. {first_idx}]" if first_idx == last_idx else f" [Ch. {first_idx}-{last_idx}]"
            )
        else:
            chapter_suffix = ""
        titled = book.title + chapter_suffix

        epub_book = epub.EpubBook()
        epub_book.set_identifier(f"{book.source}:{book.url}")
        epub_book.set_title(titled)
        epub_book.set_language("en")

        if book.author:
            epub_book.add_author(book.author)

        has_cover_image = False
        if book.cover_url:
            image = self.network_client.get_binary(str(book.cover_url))
            if image:
                epub_book.set_cover("cover.jpg", image)
                has_cover_image = True

        # shared stylesheet
        css_item = epub.EpubItem(
            uid="shared-css",
            file_name="styles/shared.css",
            media_type="text/css",
            content=_COMMON_CSS.encode(),
        )
        epub_book.add_item(css_item)

        # front-matter pages
        cover_page = self._build_cover_page(book, has_cover_image)
        info_page = self._build_info_page(book)
        for page in (cover_page, info_page):
            page.add_link(href="styles/shared.css", rel="stylesheet", type="text/css")
            epub_book.add_item(page)

        epub_chapters: list[epub.EpubHtml] = []
        for chapter in book.chapters:
            if chapter.content_html is None:
                continue  # skip stubs outside the requested range
            epub_chapter = self._chapter_to_epub(chapter)
            epub_book.add_item(epub_chapter)
            epub_chapters.append(epub_chapter)

        epub_book.toc = (
            epub.Link("cover.xhtml", "Cover", "cover"),
            epub.Link("info.xhtml", "Book Info", "info"),
            *epub_chapters,
        )
        epub_book.spine = [cover_page, info_page, "nav", *epub_chapters]
        epub_book.add_item(epub.EpubNcx())
        epub_book.add_item(epub.EpubNav())

        filename = self._safe_filename(titled)
        output_path = output_dir / f"{filename}.epub"
        epub.write_epub(str(output_path), epub_book)
        return output_path

    # ── front-matter helpers ───────────────────────────────────────────────

    @staticmethod
    def _build_cover_page(book: Book, has_cover_image: bool) -> epub.EpubHtml:
        img_html = '<img src="cover.jpg" alt="Cover"/>' if has_cover_image else ""
        author_html = f'<p class="cover-author">by {book.author}</p>' if book.author else ""
        content = (
            f"<html><body>"
            f'<div class="cover-page">'
            f"{img_html}"
            f'<h1 class="cover-title">{book.title}</h1>'
            f"{author_html}"
            f"</div>"
            f"</body></html>"
        )
        page = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
        page.content = content
        return page

    @staticmethod
    def _build_info_page(book: Book) -> epub.EpubHtml:
        included = [c for c in book.chapters if c.content_html is not None]
        if included:
            first, last = included[0], included[-1]
            chapter_range = (
                f"Ch. {first.index}"
                if first.index == last.index
                else f"Ch. {first.index} – {last.index}  ({len(included)} chapters)"
            )
        else:
            chapter_range = "N/A"

        author_row = f"<tr><td>Author</td><td>{book.author}</td></tr>" if book.author else ""
        scraped_on = datetime.date.today().isoformat()

        rows = (
            f"<tr><td>Title</td><td>{book.title}</td></tr>"
            f"{author_row}"
            f"<tr><td>Chapters</td><td>{chapter_range}</td></tr>"
            f"<tr><td>Source</td><td>{book.source}</td></tr>"
            f'<tr><td>URL</td><td><a href="{book.url}">{book.url}</a></td></tr>'
            f"<tr><td>Scraped on</td><td>{scraped_on}</td></tr>"
        )
        content = (
            f"<html><body>"
            f'<div class="info-page">'
            f"<h1>Book Info</h1>"
            f'<table class="info-table">{rows}</table>'
            f'<p class="credits">Generated by <a href="{_REPO_URL}">webnovel-scraper</a>.</p>'
            f"</div>"
            f"</body></html>"
        )
        page = epub.EpubHtml(title="Book Info", file_name="info.xhtml", lang="en")
        page.content = content
        return page

    @staticmethod
    def _chapter_to_epub(chapter: Chapter) -> epub.EpubHtml:
        file_name = f"chapter-{chapter.index:05d}.xhtml"
        content = chapter.content_html or f"<p>{chapter.title}</p>"
        cleaned = EpubBuilder._clean_html(content)

        epub_chapter = epub.EpubHtml(title=chapter.title, file_name=file_name, lang="en")
        epub_chapter.content = cleaned
        return epub_chapter

    @staticmethod
    def _clean_html(raw_html: str) -> str:
        soup = BeautifulSoup(raw_html, "html.parser")
        for node in soup(["script", "style", "iframe", "noscript"]):
            node.decompose()

        if soup.body is not None:
            body_content = "".join(str(child) for child in soup.body.children)
            return f"<html><body>{body_content}</body></html>"

        return f"<html><body>{str(soup)}</body></html>"

    @staticmethod
    def _safe_filename(value: str) -> str:
        sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "_" for ch in value)
        return "_".join(sanitized.split()).strip("_") or "book"
