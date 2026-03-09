from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from app.engine.epub import EpubBuilder
from app.engine.scraper_engine import ScraperEngine
from app.models import Book


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webnovel-scraper",
        description="Professional modular webnovel scraper and EPUB generator.",
    )

    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.0,
        metavar="SECS",
        dest="page_delay",
        help="Wait time after Playwright page load in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        metavar="N",
        help="Concurrent chapter-download workers (default: 6)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search a novel title")
    search_parser.add_argument("title", nargs="+", help="Novel title query")

    download_parser = subparsers.add_parser("download", help="Download novel into EPUB")
    download_parser.add_argument("url", help="Direct novel URL")
    download_parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory for generated EPUB",
    )
    download_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the chapter-list confirmation prompt",
    )

    debug_parser = subparsers.add_parser(
        "debug",
        help="Inspect a URL: scraper resolution, HTTP status, chapter links, pagination",
    )
    debug_parser.add_argument("url", help="URL to inspect")
    debug_parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the first 800 characters of raw HTML",
    )

    return parser


def run_search(engine: ScraperEngine, query: str, console: Console) -> int:
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Searching title catalog..."),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("search", total=None)
        result = engine.search(query)

    table = Table(title=f"Nearest matches for: {query}")
    table.add_column("#", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Source")
    table.add_column("Score", justify="right")
    table.add_column("Accepted")

    for index, candidate in enumerate(result.candidates[:10], start=1):
        accepted = "yes" if candidate.score > 85.0 else "no"
        table.add_row(
            str(index),
            candidate.book.title,
            candidate.book.source,
            f"{candidate.score:.2f}",
            accepted,
        )

    if result.candidates:
        console.print(table)
    else:
        console.print("[yellow]No search results found from registered scrapers.[/yellow]")

    if not result.accepted:
        console.print("[red]No candidate passed the 85% fuzzy-match threshold.[/red]")
        return 1

    console.print(
        f"[green]{len(result.accepted)} result(s) passed the 85% threshold and are safe to proceed.[/green]"
    )
    return 0


def run_debug(engine: ScraperEngine, url: str, show_raw: bool, console: Console) -> int:
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Fetching and inspecting URL..."),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("debug", total=None)
        info = engine.debug_url(url)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column()
    grid.add_row("URL", info.url)
    grid.add_row(
        "Scraper",
        info.scraper_name or "[red]NOT HANDLED — no scraper registered for this domain[/red]",
    )
    grid.add_row("HTTP status", str(info.status_code))
    grid.add_row(
        "Browser fallback",
        "[yellow]yes[/yellow]" if info.used_browser_fallback else "no",
    )
    grid.add_row("Detected title", info.title or "[dim]none[/dim]")
    grid.add_row("Chapter links (landing page)", str(info.chapter_count))
    grid.add_row(
        "Pagination (next page)",
        info.next_page_url or "[dim]none detected[/dim]",
    )
    console.print(Panel(grid, title="[bold]Debug Report[/bold]", expand=False))

    if show_raw:
        console.rule("[dim]HTML snippet (first 800 chars)[/dim]")
        console.print(info.raw_html_snippet)

    if info.scraper_name is None:
        console.print(
            "[yellow]Tip:[/yellow] Create a scraper in [bold]app/scrapers/[/bold] "
            "subclassing [bold]BaseScraper[/bold], then register it in "
            "[bold]ScraperEngine.with_defaults()[/bold]."
        )
        return 1

    return 0


def _confirm_chapter_list(book: Book, console: Console) -> bool:
    """Print the chapter roster and return True if the user confirms."""
    chapters = book.chapters
    total = len(chapters)

    table = Table(
        title=f"[bold]{book.title}[/bold]  ·  {total} chapters",
        show_lines=False,
        highlight=True,
    )
    table.add_column("#", style="dim", justify="right", width=6)
    table.add_column("Title", style="bold")

    preview_head = 12
    preview_tail = 5

    if total <= preview_head + preview_tail + 1:
        # Short enough to show in full.
        for ch in chapters:
            table.add_row(str(ch.index), ch.title)
    else:
        for ch in chapters[:preview_head]:
            table.add_row(str(ch.index), ch.title)
        omitted = total - preview_head - preview_tail
        table.add_row("…", f"[dim]… {omitted} more chapters …[/dim]")
        for ch in chapters[-preview_tail:]:
            table.add_row(str(ch.index), ch.title)

    console.print(table)

    answer = (
        console.input(r"[bold yellow]Proceed with download?[/bold yellow] [dim]\[y/N][/dim] ")
        .strip()
        .casefold()
    )
    return answer in {"y", "yes"}


def run_download(
    engine: ScraperEngine,
    url: str,
    output_dir: Path,
    console: Console,
    skip_confirm: bool = False,
) -> int:
    epub_builder = EpubBuilder()

    # ── Phase 1: metadata (unknown duration) ────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Fetching book metadata..."),
        console=console,
        transient=True,
    ) as spin:
        spin.add_task("", total=None)
        book_meta = engine.fetch_book_meta(url)

    total = len(book_meta.chapters)
    console.print(
        f"  [bold]{book_meta.title}[/bold]  ·  "
        f"[cyan]{total}[/cyan] chapters  ·  source: [dim]{book_meta.source}[/dim]"
    )

    # ── Confirmation ─────────────────────────────────────────────────────────
    if not skip_confirm and not _confirm_chapter_list(book_meta, console):
        console.print("[yellow]Download cancelled.[/yellow]")
        return 1

    # ── Phase 2: concurrent chapter downloads ───────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Downloading chapters", total=total, completed=0)

        def on_progress(completed: int, dl_total: int) -> None:
            progress.update(task_id, total=dl_total, completed=completed)

        book = engine.download_chapters(book_meta, on_progress=on_progress)
        output_path = epub_builder.build(book=book, output_dir=output_dir)

    console.print(f"[green]EPUB generated:[/green] {output_path}")
    console.print(
        f"[cyan]Chapters:[/cyan] {len(book.chapters)} | [cyan]Source:[/cyan] {book.source}"
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    console = Console()
    engine = ScraperEngine.with_defaults(
        page_load_delay=args.page_delay,
        max_workers=args.workers,
    )

    if args.command == "search":
        query = " ".join(args.title)
        return run_search(engine=engine, query=query, console=console)

    if args.command == "download":
        return run_download(
            engine=engine,
            url=args.url,
            output_dir=args.output,
            console=console,
            skip_confirm=args.yes,
        )

    if args.command == "debug":
        return run_debug(engine=engine, url=args.url, show_raw=args.raw, console=console)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
