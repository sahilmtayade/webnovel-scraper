"""Chapter-content validation.

Some sites occasionally serve a chapter page whose body is missing: only the
"Chapter N: Title" line and a "Translator: X" line come through.  Scrapers see
the container element and treat that as success, so the check lives here and is
shared by the engine (fetch-time retries) and the output manager (resume).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.engine.epub import _CHAPTER_HEADING_RE, _CHAPTER_META_RE, _MAX_LINE_LEN

# A chapter whose text, after dropping the heading / translator lines, is
# shorter than this is considered to have no body.
MIN_BODY_CHARS = 80

_PLACEHOLDER_PREFIXES = ("[download failed", "(chapter content unavailable")


class EmptyChapterError(Exception):
    """Raised when a fetched chapter has a title but no body text."""


def body_text(html: str | None) -> str:
    """Return the chapter text with heading, metadata and placeholder lines removed."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "iframe", "noscript"]):
        node.decompose()

    kept: list[str] = []
    for raw in soup.get_text("\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.casefold().startswith(_PLACEHOLDER_PREFIXES):
            continue
        if len(line) <= _MAX_LINE_LEN and (
            _CHAPTER_HEADING_RE.match(line) or _CHAPTER_META_RE.match(line)
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def has_body(html: str | None) -> bool:
    return len(body_text(html)) >= MIN_BODY_CHARS
