"""Per-domain AIMD adaptive rate controller with cross-run persistence.

Algorithm (mirrors TCP congestion control):
- Startup:  begin at ``min(saved / 2, CACHE_START_CAP)`` — never re-enter a ban
            by starting too fast after a previously-throttled run.
- Success (first N):  interval -= ADDITIVE_STEP  (cautious additive probe).
- Success (N+ in a row): interval *= RECOVERY_FACTOR  (fast multiplicative climb
          once the server is clearly happy; recovers from 1.6 s to 0.05 s in ~34
          clean requests instead of ~155 with the flat-step approach).
- Throttle: interval *= BACKOFF_FACTOR (multiplicative decrease),
            persist ``interval * 2`` as the next run's starting value,
            reset the consecutive-success counter to 0.
- wait():   globally serialises all requests for a domain so N concurrent
            threads don't inadvertently multiply the true rate.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

_CACHE_FILE = Path.home() / ".cache" / "webnovel-scraper" / "rates.json"

_MIN_INTERVAL: float = 0.05  # ceiling:  20 req/s
_MAX_INTERVAL: float = 30.0  # floor:    1 req/30 s
_ADDITIVE_STEP: float = 0.01  # flat step used for the first few successes
_BACKOFF_FACTOR: float = 2.0  # double the interval on every throttle signal
_RECOVERY_FACTOR: float = 0.9  # multiply interval by this after N consecutive successes
_RECOVERY_THRESHOLD: int = 5  # switch to multiplicative recovery after this many in a row
_CACHE_START_CAP: float = 5.0  # never start above this regardless of what was cached


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_all() -> dict[str, float]:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except Exception:
        return {}


def _persist(domain: str, interval: float) -> None:
    """Write the safe-start interval for *domain* to disk (best-effort)."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _load_all()
        data[domain] = interval
        _CACHE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RateController
# ---------------------------------------------------------------------------


class RateController:
    """AIMD adaptive rate limiter for a single domain."""

    def __init__(self, domain: str, default_interval: float = _MIN_INTERVAL) -> None:
        self.domain = domain
        self._lock = threading.Lock()

        saved = _load_all().get(domain)
        # Start from the cached safe interval BUT cap it so a previous ban can't
        # force the next run to crawl at e.g. 30 s/req from the very first request.
        if saved is not None:
            self._interval: float = max(_MIN_INTERVAL, min(saved / 2.0, _CACHE_START_CAP))
        else:
            self._interval: float = default_interval
        self._last_sent: float = 0.0  # monotonic timestamp of the last request slot
        self._consecutive_successes: int = 0  # reset to 0 on every throttle signal

    @property
    def current_interval(self) -> float:
        with self._lock:
            return self._interval

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def wait(self, on_sleep: Callable[[float], None] | None = None) -> None:
        """Block until the next request slot is available, then claim it.

        The lock is held only while computing and reserving the deadline so
        concurrent callers queue up without sleeping under the lock.

        Parameters
        ----------
        on_sleep:
            Called with the sleep duration (seconds) *before* sleeping begins.
            Useful for updating live status displays.
        """
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._last_sent + self._interval - now)
            self._last_sent = now + sleep_for

        if sleep_for > 0.0:
            if on_sleep is not None:
                on_sleep(sleep_for)
            time.sleep(sleep_for)

    def success(self) -> None:
        """Decrease the inter-request interval (increase rate).

        For the first ``_RECOVERY_THRESHOLD`` consecutive successes, use a flat
        additive step — conservative while still probing.  Once the server has
        been happy for a sustained run, switch to multiplicative recovery so we
        climb back from a high interval quickly without risking another ban.
        """
        with self._lock:
            self._consecutive_successes += 1
            if self._consecutive_successes >= _RECOVERY_THRESHOLD:
                # Multiplicative climb: 1.6 s → 0.05 s in ~34 requests.
                self._interval = max(_MIN_INTERVAL, self._interval * _RECOVERY_FACTOR)
            else:
                # Additive probe: cautious while we have few consecutive wins.
                self._interval = max(_MIN_INTERVAL, self._interval - _ADDITIVE_STEP)

    def throttled(self) -> None:
        """Multiplicatively increase the interval and persist a conservative value."""
        with self._lock:
            self._consecutive_successes = 0
            self._interval = min(_MAX_INTERVAL, self._interval * _BACKOFF_FACTOR)
            to_save = self._interval * 2.0
            domain = self.domain
        # Persist outside the lock to avoid holding it during I/O.
        _persist(domain, to_save)
