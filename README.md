# webnovel-scraper

Professional modular webnovel scraper and EPUB generator.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
uv sync
uv run playwright install chromium
```

## How to run

All commands are run via `uv run webnovel-scraper`.

### Search

Search for a novel title and see fuzzy-matched candidates:

```bash
uv run webnovel-scraper search "shadow slave"
```

### Download

Download a novel and generate an EPUB in the `output/` directory:

```bash
uv run webnovel-scraper download https://novellive.app/book/shadow-slave
```

Options:

```
--output PATH   Output directory for the generated EPUB (default: output/)
--yes, -y       Skip the chapter-list confirmation prompt
```

### Debug

Inspect how a URL is resolved — scraper, HTTP status, chapter count, pagination:

```bash
uv run webnovel-scraper debug https://novellive.app/book/shadow-slave
```

Add `--raw` to also print the first 800 characters of raw HTML.

### Global flags

These flags apply to every subcommand and go **before** the subcommand name:

```
--page-delay SECS   Wait time after Playwright page load in seconds (default: 1.0)
--workers N         Concurrent chapter-download workers (default: 6)
```

Example — download with more workers and no confirmation prompt:

```bash
uv run webnovel-scraper --workers 10 download -y https://novellive.app/book/shadow-slave
```

## Development

### Setup pre-commit

```bash
uv run pre-commit install
```

### Lint / format / type-check

```bash
uv run ruff check app/
uv run ruff format app/
uv run ty check app/
```
