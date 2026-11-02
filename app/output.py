"""Output manager: find existing EPUBs for a book and read their chapters back.

EPUBs written by :class:`~app.engine.epub.EpubBuilder` carry the book identity
(``source:url``) in their OPF and one ``chapter-NNNNN.xhtml`` per chapter, where
NNNNN is the 1-based chapter index.  That is enough to resume into an existing
file without any sidecar state.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from app.engine.content import has_body
from app.models import Book

_CHAPTER_ENTRY_RE = re.compile(r"(?:^|/)chapter-(\d+)\.xhtml$")
_IDENTIFIER_RE = re.compile(r"<dc:identifier[^>]*>(.*?)</dc:identifier>", re.S)


def book_identifier(book: Book) -> str:
    return f"{book.source}:{book.url}"


@dataclass(frozen=True)
class ExistingEpub:
    path: Path
    total: int  # chapters stored in the file
    complete: int  # of those, chapters that have real body text

    @property
    def incomplete(self) -> int:
        return self.total - self.complete


def _epub_identifier(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".opf"):
                    match = _IDENTIFIER_RE.search(zf.read(name).decode("utf-8", "replace"))
                    if match:
                        return match.group(1).strip()
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def read_chapters(path: Path) -> dict[int, str]:
    """Return ``{chapter_index: body_inner_html}`` stored in an EPUB."""
    chapters: dict[int, str] = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            match = _CHAPTER_ENTRY_RE.search(name)
            if not match:
                continue
            soup = BeautifulSoup(zf.read(name).decode("utf-8", "replace"), "html.parser")
            body = soup.body
            html = "".join(str(c) for c in body.children) if body is not None else str(soup)
            chapters[int(match.group(1))] = html
    return chapters


def find_existing(book: Book, output_dir: Path) -> list[ExistingEpub]:
    """EPUBs in *output_dir* that were generated for *book*, newest first."""
    if not output_dir.is_dir():
        return []
    wanted = book_identifier(book)
    found: list[ExistingEpub] = []
    for path in sorted(output_dir.glob("*.epub"), key=lambda p: p.stat().st_mtime, reverse=True):
        if _epub_identifier(path) != wanted:
            continue
        chapters = read_chapters(path)
        found.append(
            ExistingEpub(
                path=path,
                total=len(chapters),
                complete=sum(1 for html in chapters.values() if has_body(html)),
            )
        )
    return found


def merge_existing(book: Book, path: Path) -> tuple[Book, frozenset[int]]:
    """Load chapter content from *path* onto *book*'s fresh stubs.

    Returns the merged book and the set of indices that were filled from the
    file (those are already cleaned and must not be re-processed).
    """
    stored = read_chapters(path)
    preserved = frozenset(i for i in stored if 1 <= i <= len(book.chapters))
    chapters = [
        ch.model_copy(update={"content_html": stored[ch.index]}) if ch.index in preserved else ch
        for ch in book.chapters
    ]
    return book.model_copy(update={"chapters": chapters}), preserved
