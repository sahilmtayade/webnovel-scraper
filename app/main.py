from __future__ import annotations

import argparse
import threading
import time
from collections import deque
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from app.engine.epub import EpubBuilder
from app.engine.scraper_engine import ScraperEngine
from app.engine.types import DownloadTick
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
    parser.add_argument(
        "--max-browser-sessions",
        type=int,
        default=3,
        metavar="N",
        dest="max_browser_sessions",
        help="Max simultaneous headed browser windows for bot challenges (default: 3)",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)

    search_parser = subparsers.add_parser("search", help="Search a novel title")
    search_parser.add_argument("title", nargs="+", help="Novel title query")
    search_parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory if you proceed to download (default: output)",
    )
    search_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the chapter-list confirmation prompt when downloading",
    )
    search_parser.add_argument(
        "--start",
        type=int,
        default=None,
        metavar="N",
        help="First chapter to download (1-based, inclusive)",
    )
    search_parser.add_argument(
        "--end",
        type=int,
        default=None,
        metavar="N",
        help="Last chapter to download (1-based, inclusive)",
    )

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
    download_parser.add_argument(
        "--start",
        type=int,
        default=None,
        metavar="N",
        help="First chapter to download (1-based, inclusive)",
    )
    download_parser.add_argument(
        "--end",
        type=int,
        default=None,
        metavar="N",
        help="Last chapter to download (1-based, inclusive)",
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


def run_search(
    engine: ScraperEngine,
    query: str,
    console: Console,
    output_dir: Path | None = None,
    skip_confirm: bool = False,
    start_chapter: int | None = None,
    end_chapter: int | None = None,
) -> int:
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Searching title catalog..."),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("search", total=None)
        result = engine.search(query)

    candidates = result.candidates[:10]

    table = Table(title=f"Nearest matches for: [bold]{query}[/bold]")
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("Title", style="bold")
    table.add_column("Source", style="cyan")
    table.add_column("Score", justify="right")

    for index, candidate in enumerate(candidates, start=1):
        score_style = "green" if candidate.score > 85.0 else "yellow"
        table.add_row(
            str(index),
            candidate.book.title,
            candidate.book.source,
            f"[{score_style}]{candidate.score:.1f}[/{score_style}]",
        )

    if candidates:
        console.print(table)
    else:
        console.print("[yellow]No search results found from registered scrapers.[/yellow]")
        return 1

    # ── Selection prompt ─────────────────────────────────────────────────────
    while True:
        raw = console.input(
            "[bold yellow]Enter # to download, or [dim]q[/dim] to quit:[/bold yellow] "
        ).strip()
        if raw.casefold() in {"q", "quit", ""}:
            return 0
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(candidates):
                selected = candidates[choice - 1]
                break
        console.print(f"[red]Please enter a number between 1 and {len(candidates)}.[/red]")

    console.print(
        f"  Selected: [bold]{selected.book.title}[/bold] ([cyan]{selected.book.source}[/cyan])"
    )
    return run_download(
        engine=engine,
        url=selected.book.url,
        output_dir=output_dir or Path("output"),
        console=console,
        skip_confirm=skip_confirm,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
    )


def _ask(console: Console, prompt: str, default: str = "") -> str:
    """Prompt the user and return stripped input; return *default* on empty."""
    suffix = f" [dim](default: {default})[/dim]" if default else ""
    raw = console.input(f"[bold yellow]{prompt}[/bold yellow]{suffix}: ").strip()
    return raw if raw else default


def _ask_int(
    console: Console,
    prompt: str,
    default: int | None = None,
) -> int | None:
    """Prompt for an integer; return *default* on empty, re-ask on bad input."""
    suffix = (
        " [dim](Enter to skip)[/dim]" if default is None else f" [dim](default: {default})[/dim]"
    )
    while True:
        raw = console.input(f"[bold yellow]{prompt}[/bold yellow]{suffix}: ").strip()
        if not raw:
            return default
        if raw.lstrip("-").isdigit():
            return int(raw)
        console.print("[red]Please enter a whole number.[/red]")


def _run_settings_menu(engine: ScraperEngine, console: Console) -> ScraperEngine:
    """Show and optionally edit runtime settings; returns a (possibly new) engine."""
    tbl = Table(title="Current settings", show_header=True)
    tbl.add_column("Setting", style="bold cyan")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Workers", str(engine._max_workers))
    tbl.add_row("Page-load delay (s)", f"{engine._client.page_load_delay:.1f}")
    tbl.add_row("Max browser sessions", str(engine._client.max_browser_sessions))
    console.print(tbl)
    console.print("[dim]Press Enter to keep the current value.[/dim]\n")

    new_workers = _ask_int(console, "Workers", default=engine._max_workers)
    delay_raw = _ask(
        console,
        "Page-load delay (s)",
        default=str(engine._client.page_load_delay),
    )
    try:
        new_delay = float(delay_raw)
    except ValueError:
        new_delay = engine._client.page_load_delay
    new_browsers = _ask_int(
        console, "Max browser sessions", default=engine._client.max_browser_sessions
    )

    changed = (
        new_workers != engine._max_workers
        or new_delay != engine._client.page_load_delay
        or new_browsers != engine._client.max_browser_sessions
    )
    if not changed:
        console.print("[dim]No changes.[/dim]")
        return engine

    rebuilt = ScraperEngine.with_defaults(
        page_load_delay=new_delay,
        max_workers=new_workers or engine._max_workers,
        max_browser_sessions=new_browsers or engine._client.max_browser_sessions,
    )
    console.print("[green]Settings updated.[/green]")
    return rebuilt


def run_interactive(engine: ScraperEngine, console: Console) -> int:
    """Full interactive session: search → pick → configure range → download."""

    # Use a mutable reference so the Settings menu can rebuild the engine.
    current_engine = engine

    # ── Welcome banner ───────────────────────────────────────────────────────
    provider_lines = "\n".join(
        f"  [cyan]•[/cyan] [bold]{s.site_name}[/bold]" for s in current_engine.scrapers
    )
    console.print(
        Panel(
            f"[bold white]Webnovel Scraper[/bold white]\n\n"
            f"Active providers:\n{provider_lines}\n\n"
            f"[dim]Tip: pass [bold]--workers N[/bold] to control concurrency[/dim]",
            border_style="cyan",
            expand=False,
        )
    )

    while True:
        console.print()
        console.print("[bold]  1[/bold]  Search for a novel")
        console.print("[bold]  2[/bold]  View active providers")
        console.print("[bold]  3[/bold]  Settings")
        console.print("[bold]  q[/bold]  Quit")
        console.print()

        choice = console.input("[bold yellow]>[/bold yellow] ").strip().casefold()

        if choice in {"q", "quit", ""}:
            return 0

        if choice == "2":
            tbl = Table(title="Active providers", show_header=True)
            tbl.add_column("Site name", style="bold cyan")
            tbl.add_column("Domains")
            for s in current_engine.scrapers:
                tbl.add_row(s.site_name, ", ".join(s.domains))
            console.print(tbl)
            continue

        if choice == "3":
            current_engine = _run_settings_menu(current_engine, console)
            continue

        if choice != "1":
            console.print("[red]Please enter 1, 2, 3, or q.[/red]")
            continue

        # ── Search ────────────────────────────────────────────────────────────
        query = _ask(console, "Search query")
        if not query:
            continue

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]Searching..."),
            console=console,
            transient=True,
        ) as prog:
            prog.add_task("", total=None)
            result = current_engine.search(query)

        candidates = result.candidates[:10]
        if not candidates:
            console.print("[yellow]No results found.[/yellow]")
            continue

        tbl = Table(title=f"Results for: [bold]{query}[/bold]")
        tbl.add_column("#", style="dim", justify="right", width=3)
        tbl.add_column("Title", style="bold")
        tbl.add_column("Source", style="cyan")
        tbl.add_column("Score", justify="right")
        for i, c in enumerate(candidates, start=1):
            score_style = "green" if c.score > 85.0 else "yellow"
            tbl.add_row(
                str(i),
                c.book.title,
                c.book.source,
                f"[{score_style}]{c.score:.1f}[/{score_style}]",
            )
        console.print(tbl)

        # ── Pick result ──────────────────────────────────────────────────────
        selected = None
        while True:
            raw = (
                console.input(
                    "[bold yellow]Enter # to select, "
                    "[dim]s[/dim] to search again, "
                    "[dim]q[/dim] to quit[/bold yellow]: "
                )
                .strip()
                .casefold()
            )
            if raw in {"q", "quit"}:
                return 0
            if raw in {"s", "search", ""}:
                break
            if raw.isdigit():
                n = int(raw)
                if 1 <= n <= len(candidates):
                    selected = candidates[n - 1]
                    break
            console.print(f"[red]Enter a number between 1 and {len(candidates)}.[/red]")

        if selected is None:
            continue

        console.print(f"\n  [bold]{selected.book.title}[/bold] [dim]— {selected.book.source}[/dim]")

        # ── Chapter range ────────────────────────────────────────────────────
        console.print("[dim]Leave start/end blank to download the whole novel.[/dim]")
        start_ch = _ask_int(console, "Start chapter")
        end_ch = _ask_int(console, "End chapter  ")

        # ── Output directory ─────────────────────────────────────────────────
        out_raw = _ask(console, "Output directory", default="output")
        output_dir = Path(out_raw)

        rc = run_download(
            engine=current_engine,
            url=selected.book.url,
            output_dir=output_dir,
            console=console,
            skip_confirm=False,
            start_chapter=start_ch,
            end_chapter=end_ch,
        )

        console.print()
        again = (
            console.input("[bold yellow]Download another novel? [dim][y/N][/dim][/bold yellow]: ")
            .strip()
            .casefold()
        )
        if again not in {"y", "yes"}:
            return rc

    return 0


def run_debug(
    engine: ScraperEngine,
    url: str,
    show_raw: bool,
    console: Console,
) -> int:
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
    start_chapter: int | None = None,
    end_chapter: int | None = None,
) -> int:
    epub_builder = EpubBuilder()

    # ── Phase 1: metadata ────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Fetching book metadata..."),
        console=console,
        transient=True,
    ) as spin:
        spin.add_task("", total=None)
        book_meta = engine.fetch_book_meta(url)

    total_chapters = len(book_meta.chapters)
    lo = (start_chapter - 1) if start_chapter is not None else 0
    hi = end_chapter if end_chapter is not None else total_chapters
    hi = min(hi, total_chapters)
    total = hi - lo  # number of chapters we'll actually download

    range_note = (
        f" [dim](chapters {lo + 1}–{hi})[/dim]"
        if (start_chapter is not None or end_chapter is not None)
        else ""
    )
    console.print(
        f"  [bold]{book_meta.title}[/bold]  ·  "
        f"[cyan]{total_chapters}[/cyan] chapters total{range_note}  ·  "
        f"source: [dim]{book_meta.source}[/dim]"
    )

    # ── Confirmation ─────────────────────────────────────────────────────────
    if not skip_confirm and not _confirm_chapter_list(book_meta, console):
        console.print("[yellow]Download cancelled.[/yellow]")
        return 1

    # ── Phase 2: concurrent chapter downloads ───────────────────────────────
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Downloading[/bold cyan]"),
        BarColumn(bar_width=36),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    task_id = progress.add_task("", total=total, completed=0)

    log: deque[tuple[str, str, str]] = deque(maxlen=12)
    start_time = time.monotonic()
    status_msg: list[str] = [""]  # mutable singleton updated during retry waits
    retrying_indices: set[int] = set()  # chapters currently in retry queue
    latest_tick: list[DownloadTick | None] = [None]  # most recent tick for background refresh
    client = engine._client  # for worker-state access in display

    def _make_display(tick: DownloadTick | None = None) -> Group:
        elapsed = max(time.monotonic() - start_time, 0.001)
        done = tick.succeeded if tick else 0
        speed = done / (elapsed / 60) if done else 0.0
        failed_n = tick.failed if tick else 0
        rl_n = tick.rate_limited if tick else 0
        retrying_n = len(retrying_indices)

        stats = Text()
        stats.append(f"  ✓ {done} done ", style="green")
        stats.append(f" ✗ {failed_n} failed ", style="red" if failed_n else "dim")
        stats.append(f" ↻ {retrying_n} retrying ", style="yellow" if retrying_n else "dim")
        stats.append(f" ⚡ {rl_n} rate-limited ", style="yellow" if rl_n else "dim")
        stats.append(f" {speed:.1f} ch/min", style="cyan")
        # Worker badge is filled in later (after building the snapshot).

        # ── Per-thread worker status ───────────────────────────────────────
        now_m = time.monotonic()
        with client._ws_lock:
            ws_snapshot = [
                (w.worker_num, w.label, w.state, w.sleep_until)
                for w in sorted(client.worker_states.values(), key=lambda w: w.worker_num)
            ]

        fetching = [(wnum, lbl) for (wnum, lbl, st, _) in ws_snapshot if st == "fetch"]
        sleeping = [(wnum, lbl, su) for (wnum, lbl, st, su) in ws_snapshot if st == "sleep"]
        w_active = len(fetching) + len(sleeping)
        w_max = engine._max_workers

        # Update stats badge with live count.
        if w_active > 0 and w_active < w_max:
            stats.append(f"  [{w_active}/{w_max} workers — rate-limited]", style="yellow")
        elif w_active > 0:
            stats.append(f"  [{w_active} workers]", style="dim")
        else:
            stats.append("  [starting…]", style="dim")

        # Build workers widget.
        if not ws_snapshot:
            workers_body: Table | Text = Text("  workers starting…", style="dim")
        elif fetching or not sleeping:
            # Normal per-row view (at least one thread actively fetching).
            workers_body = Table.grid(padding=(0, 1))
            workers_body.add_column(width=3, justify="right")
            workers_body.add_column(width=2)
            workers_body.add_column(min_width=35)
            workers_body.add_column(width=6, justify="right")
            for wnum, lbl, st, su in ws_snapshot:
                if st == "sleep":
                    remaining = max(0.0, su - now_m)
                    workers_body.add_row(
                        Text(f"W{wnum}", style="dim"),
                        Text("⏸", style="yellow"),
                        Text((lbl or "…")[:48], style="dim yellow"),
                        Text(f"{remaining:.1f}s", style="yellow"),
                    )
                elif st == "fetch":
                    workers_body.add_row(
                        Text(f"W{wnum}", style="dim"),
                        Text("↓", style="bold green"),
                        Text((lbl or "…")[:48], style="white"),
                        Text(""),
                    )
                else:
                    workers_body.add_row(
                        Text(f"W{wnum}", style="dim"),
                        Text("○", style="dim"),
                        Text("idle", style="dim"),
                        Text(""),
                    )
        else:
            # All non-idle workers are sleeping — show a single countdown.
            earliest = min(sleeping, key=lambda x: x[2])  # (wnum, lbl, su)
            remaining = max(0.0, earliest[2] - now_m)
            who = earliest[1] or "…"
            n_queued = len(sleeping)
            workers_body = Text.from_markup(
                f"  ⏸  Rate-limited — next request in "
                f"[bold yellow]{remaining:.1f}s[/bold yellow]"
                f"  [dim]({n_queued} thread{'s' if n_queued > 1 else ''} queued"
                f" · {who[:40]})[/dim]"
            )

        # ── Recent-activity log ────────────────────────────────────────────
        recent = Table.grid(padding=(0, 1))
        recent.add_column(width=2)
        recent.add_column()
        for entry in log:
            icon, style, text = entry
            recent.add_row(Text(icon, style=style), Text(text, style="dim"))

        parts: list = [
            progress,
            stats,
            Rule("Workers", style="dim"),
            workers_body,
            Rule("Recent", style="dim"),
            recent,
        ]
        if status_msg[0]:
            parts.insert(1, Text(f"  {status_msg[0]}", style="bold yellow"))

        return Group(*parts)

    def on_tick(tick: DownloadTick) -> None:
        latest_tick[0] = tick
        progress.update(task_id, completed=tick.succeeded + tick.failed)
        is_final = tick.error is None or tick.attempt == tick.max_attempts

        if tick.error is None:
            retrying_indices.discard(tick.chapter_index)
            suffix = f" (retry {tick.attempt})" if tick.attempt > 1 else ""
            log.append(("✓", "green", f"{tick.chapter_title}{suffix}"))
        elif not is_final:
            # Will be retried — add to in-flight set
            retrying_indices.add(tick.chapter_index)
            short_err = tick.error[:50] + "…" if len(tick.error) > 50 else tick.error
            log.append(("↻", "yellow", f"{tick.chapter_title} — {short_err}"))
        else:
            # Permanent failure (all retries exhausted)
            retrying_indices.discard(tick.chapter_index)
            short_err = tick.error[:50] + "…" if len(tick.error) > 50 else tick.error
            log.append(("✗", "red", f"{tick.chapter_title} — {short_err}"))

        live.update(_make_display(tick))

    def on_status(msg: str) -> None:
        status_msg[0] = msg
        live.update(_make_display())

    _stop_refresh = threading.Event()

    def _refresh_loop() -> None:
        """Drive live countdown updates at ~10 Hz whenever any worker is sleeping."""
        while not _stop_refresh.wait(0.1):
            with client._ws_lock:
                has_sleeping = any(w.state == "sleep" for w in client.worker_states.values())
            if has_sleeping:
                live.update(_make_display(latest_tick[0]))

    _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True, name="display-refresh")

    with Live(_make_display(), console=console, refresh_per_second=10) as live:
        _refresh_thread.start()
        try:
            book = engine.download_chapters(
                book_meta,
                on_tick=on_tick,
                on_status=on_status,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
            )
        finally:
            _stop_refresh.set()
            _refresh_thread.join(timeout=0.5)

    output_path = epub_builder.build(book=book, output_dir=output_dir)

    placeholder = "<p>[Download failed"
    succeeded = sum(
        1 for ch in book.chapters if ch.content_html and not ch.content_html.startswith(placeholder)
    )
    # Only count chapters we attempted (in range); stubs outside range have content_html=None
    fetched_count = sum(1 for ch in book.chapters if ch.content_html is not None)
    failed_count = fetched_count - succeeded
    console.print(f"[green]EPUB generated:[/green] {output_path}")
    console.print(
        f"[green]✓ {succeeded} succeeded[/green]  "
        + (f"[red]✗ {failed_count} failed[/red]  " if failed_count else "")
        + f"[cyan]Source:[/cyan] {book.source}"
    )
    return 0 if failed_count == 0 else 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    console = Console()
    engine = ScraperEngine.with_defaults(
        page_load_delay=args.page_delay,
        max_workers=args.workers,
        max_browser_sessions=args.max_browser_sessions,
    )

    if args.command is None:
        try:
            return run_interactive(engine=engine, console=console)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            return 0

    if args.command == "search":
        query = " ".join(args.title)
        return run_search(
            engine=engine,
            query=query,
            console=console,
            output_dir=args.output,
            skip_confirm=args.yes,
            start_chapter=args.start,
            end_chapter=args.end,
        )

    if args.command == "download":
        return run_download(
            engine=engine,
            url=args.url,
            output_dir=args.output,
            console=console,
            skip_confirm=args.yes,
            start_chapter=args.start,
            end_chapter=args.end,
        )

    if args.command == "debug":
        return run_debug(engine=engine, url=args.url, show_raw=args.raw, console=console)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
