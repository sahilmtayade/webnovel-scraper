from __future__ import annotations

import datetime
import re
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
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

.chapter-heading {
    margin: 0 0 0.4em;
    font-size: 1.1em;
    font-weight: 700;
    color: #f2e9c5;
    text-transform: none;
    letter-spacing: 0.02em;
}

.chapter-meta {
    margin: 0 0 1rem;
    color: #b8a88f;
    font-size: 0.95em;
    line-height: 1.4;
}

.chapter-meta-item {
    display: inline-block;
    margin-right: 0.8em;
}

.chapter-meta-item:not(:last-child)::after {
    content: "•";
    margin-left: 0.8em;
    color: #7c6d56;
}
"""

# ── chapter-heading / metadata detection ────────────────────────────────────
#
# Scraped chapter HTML shows up in wildly different shapes depending on the
# source site:
#   - everything crammed into one <p> separated by <br> tags
#   - one <p>/<div> per field
#   - the "Chapter N" title inside an <h1>/<h2> instead of a <p>
#   - translator/editor names wrapped in <strong>/<a>/<span> tags
#
# The matching below is line-based (rather than "does this whole node's text
# match one big regex") specifically so all of the above are handled the
# same way.
_CHAPTER_HEADING_RE = re.compile(r"^(chapter\s*\d+|prologue|epilogue|interlude)\b", re.I)
_CHAPTER_META_RE = re.compile(
    r"^(translator|tl|editor|proofreader|pr|author|volume|release(?:d)?|published)\s*:"
    r"|^(translated|edited|proofread)\s+by\b",
    re.I,
)
# Splits a line like "Translator: X  Editor: Y" (no <br>, just run together)
# into separate "Translator: X" / "Editor: Y" pieces for their own bullets.
_META_SPLIT_RE = re.compile(
    r"(?=\b(?:translator|tl|editor|proofreader|pr|author|volume|release(?:d)?|published)\s*:)",
    re.I,
)
_HEADER_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "chapter-c",
    "chapter-content",
    "header",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_MAX_HEADER_NODES = 6
_MAX_LINE_LEN = 200  # a "line" longer than this is prose, not a title/metadata field


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

    def _chapter_to_epub(self, chapter: Chapter) -> epub.EpubHtml:
        file_name = f"chapter-{chapter.index:05d}.xhtml"
        content = chapter.content_html or f"<p>{chapter.title}</p>"
        cleaned = self._clean_html(content)

        epub_chapter = epub.EpubHtml(title=chapter.title, file_name=file_name, lang="en")
        epub_chapter.content = cleaned
        return epub_chapter

    def _clean_html(self, raw_html: str) -> str:
        soup = BeautifulSoup(raw_html, "html.parser")
        for node in soup(["script", "style", "iframe", "noscript"]):
            node.decompose()

        self._strip_noise(soup)
        self._strip_ad_links(soup)
        self._stylize_chapter_metadata(soup)

        if soup.body is not None:
            body_content = "".join(str(child) for child in soup.body.children)
            return f"<html><body>{body_content}</body></html>"

        return f"<html><body>{str(soup)}</body></html>"

    @staticmethod
    def _strip_noise(soup: BeautifulSoup) -> None:
        """Drop HTML comments (often dead ad-script snippets) and the empty
        ad-placeholder <div>s many scrapers leave behind, e.g.
        ``<div style="..."><div id="bg-ssp-6327"></div></div>`` with no text
        and no image. These carry no content but, left in place, they break
        the "single wrapper child" check _content_root relies on to find
        where the real paragraphs live, and they're pointless bloat in the
        final EPUB regardless.
        """
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        changed = True
        while changed:
            changed = False
            for div in soup.find_all("div"):
                if div.find("img") is not None:
                    continue
                if not div.get_text(strip=True) and div.find(True) is None:
                    div.decompose()
                    changed = True

    @staticmethod
    def _strip_ad_links(soup: BeautifulSoup) -> None:
        """Remove sponsored/affiliate ad links some scraped pages leave behind
        (e.g. a bare <a rel="sponsored nofollow" href="..."/> with no visible
        text), and collapse any wrapper element left empty afterward."""
        for a_tag in soup.find_all("a"):
            if a_tag.get_text(strip=True):
                continue  # has visible text - leave it, could be legitimate content
            rel = " ".join(a_tag.get("rel", []) or []).lower()
            href = (a_tag.get("href") or "").lower()
            if "sponsored" in rel or "nofollow" in rel or "/ads" in href or "doubleclick" in href:
                parent = a_tag.parent
                a_tag.decompose()
                while (
                    parent is not None
                    and isinstance(parent, Tag)
                    and parent.name not in {"body", "html"}
                    and not parent.get_text(strip=True)
                    and parent.find(True) is None
                ):
                    grandparent = parent.parent
                    parent.decompose()
                    parent = grandparent

    @staticmethod
    def _node_lines(node: Tag) -> list[str]:
        """Flatten a node's text into lines.

        Treats <br> as a line break; everything else (including text inside
        nested <strong>/<a>/<span>/etc. tags) is joined onto the current
        line. This is what lets "Chapter 1<br>Translator: X" in a single <p>
        and "<strong>Translator:</strong> <a>X</a>" both resolve to the
        same plain-text line, instead of breaking on nested tags or getting
        split into fragments the way BeautifulSoup's own get_text(separator=...)
        would.
        """
        lines: list[str] = []
        current: list[str] = []

        def walk(n: Tag) -> None:
            for child in n.children:
                if isinstance(child, Tag) and child.name == "br":
                    lines.append("".join(current).strip())
                    current.clear()
                elif isinstance(child, Tag):
                    walk(child)
                elif isinstance(child, NavigableString):
                    current.append(str(child))

        walk(node)
        lines.append("".join(current).strip())
        return [ln for ln in lines if ln]

    @staticmethod
    def _content_root(body: Tag) -> Tag:
        """Unwrap trivial single-child wrapper elements.

        Scraped chapter markup very often looks like
        ``<div id="chapter-content"><p>Chapter 1</p><p>...</p>...</div>``
        rather than putting paragraphs directly under <body>. If we only
        scan body's immediate children we'd see just that one wrapper div
        and never find the heading/metadata paragraphs inside it. This
        walks down through single-child div/section/article wrappers until
        it finds the level where the actual paragraphs live as siblings.
        """
        root = body
        while True:
            kids = [
                c for c in root.contents if not (isinstance(c, NavigableString) and not c.strip())
            ]
            if (
                len(kids) == 1
                and isinstance(kids[0], Tag)
                and kids[0].name
                in {
                    "div",
                    "section",
                    "article",
                }
            ):
                root = kids[0]
                continue
            break
        return root

    def _stylize_chapter_metadata(self, soup: BeautifulSoup) -> None:
        """Turn a leading "Chapter N: Title" / "Translator: X" block into a
        styled <h2 class="chapter-heading"> + <p class="chapter-meta">,
        instead of leaving it as indistinguishable body paragraphs.
        """
        container: Tag | None = soup.body
        if container is None:
            # Some scrapers return an HTML fragment rooted at a <div> with no
            # <html>/<body> wrapper. In that case, use the first top-level tag.
            for child in soup.contents:
                if isinstance(child, Tag):
                    container = child
                    break
        if container is None:
            return

        root = self._content_root(container)
        body_children = [
            node for node in root.contents if not (isinstance(node, str) and node.strip() == "")
        ]
        if not body_children:
            return

        candidate_nodes: list[Tag] = []
        heading_line: str | None = None
        meta_lines: list[str] = []

        for node in body_children[:_MAX_HEADER_NODES]:
            if isinstance(node, NavigableString):
                break  # stray top-level text before any recognizable block - stop
            if not isinstance(node, Tag) or node.name not in _HEADER_BLOCK_TAGS:
                break

            node_lines = self._node_lines(node)

            if not node_lines:
                # empty spacer node (e.g. a blank <div>) - harmless, keep scanning
                candidate_nodes.append(node)
                continue

            if any(len(ln) > _MAX_LINE_LEN for ln in node_lines):
                break  # this node is prose, not a title/metadata field

            if heading_line is None:
                if not _CHAPTER_HEADING_RE.match(node_lines[0]):
                    return  # no recognizable chapter heading up front - leave content as-is
                heading_line = node_lines[0]
                remaining = node_lines[1:]
            else:
                remaining = node_lines

            if not all(_CHAPTER_META_RE.match(ln) for ln in remaining):
                break  # first line that isn't metadata ends the header block

            for ln in remaining:
                # a site may put "Translator: X  Editor: Y" on one line with
                # no <br> between them - split those into separate bullets
                parts = [p.strip() for p in _META_SPLIT_RE.split(ln) if p.strip()]
                meta_lines.extend(parts if parts else [ln])
            candidate_nodes.append(node)

        if heading_line is None or not candidate_nodes:
            return

        heading_tag = soup.new_tag("h2", attrs={"class": "chapter-heading"})
        heading_tag.string = heading_line
        new_nodes: list[Tag] = [heading_tag]

        if meta_lines:
            meta_tag = soup.new_tag("p", attrs={"class": "chapter-meta"})
            for line in meta_lines:
                item = soup.new_tag("span", attrs={"class": "chapter-meta-item"})
                item.string = line
                meta_tag.append(item)
            new_nodes.append(meta_tag)

        first = candidate_nodes[0]
        for node in candidate_nodes[1:]:
            node.extract()
        first.replace_with(*new_nodes)

    @staticmethod
    def _safe_filename(value: str) -> str:
        sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "_" for ch in value)
        return "_".join(sanitized.split()).strip("_") or "book"
