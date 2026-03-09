from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

from app.engine.client import NetworkClient
from app.models import Book, Chapter


class EpubBuilder:
    def __init__(self, network_client: NetworkClient | None = None) -> None:
        self.network_client = network_client or NetworkClient()

    def build(self, book: Book, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)

        epub_book = epub.EpubBook()
        epub_book.set_identifier(f"{book.source}:{book.url}")
        epub_book.set_title(book.title)
        epub_book.set_language("en")

        if book.author:
            epub_book.add_author(book.author)

        if book.cover_url:
            image = self.network_client.get_binary(str(book.cover_url))
            if image:
                epub_book.set_cover("cover.jpg", image)

        epub_chapters: list[epub.EpubHtml] = []
        for chapter in book.chapters:
            epub_chapter = self._chapter_to_epub(chapter)
            epub_book.add_item(epub_chapter)
            epub_chapters.append(epub_chapter)

        epub_book.toc = tuple(epub_chapters)
        epub_book.spine = ["nav", *epub_chapters]
        epub_book.add_item(epub.EpubNcx())
        epub_book.add_item(epub.EpubNav())

        filename = self._safe_filename(book.title)
        output_path = output_dir / f"{filename}.epub"
        epub.write_epub(str(output_path), epub_book)
        return output_path

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
