"""Per-domain AIMD adaptive rate controller with cross-run persistence.

Algorithm (mirrors TCP congestion control):
- Startup:  begin at ``saved_peak / 2`` — conservative probe from known-safe point.
- Success:  interval -= ADDITIVE_STEP  (additive increase of request rate).
- Throttle: interval *= BACKOFF_FACTOR (multiplicative decrease of request rate),
            then persist ``interval * 2`` as the next run's starting value.
- wait():   globally serialises all requests for a domain so N concurrent
            threads don't inadvertently multiply the true rate.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_CACHE_FILE = Path.home() / ".cache" / "webnovel-scraper" / "rates.json"

_MIN_INTERVAL: float = 0.05  # ceiling:  20 req/s
_MAX_INTERVAL: float = 30.0  # floor:    1 req/30 s
_ADDITIVE_STEP: float = 0.01  # shave 10 ms off the interval per successful request
_BACKOFF_FACTOR: float = 2.0  # double the interval on every throttle signal


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

    def __init__(self, domain: str, default_interval: float = 1.0) -> None:
        self.domain = domain
        self._lock = threading.Lock()

        saved = _load_all().get(domain)
        # Start at half the last known-safe interval; probe upward from there.
        self._interval: float = max(
            _MIN_INTERVAL,
            ((saved if saved is not None else default_interval) / 2.0),
        )
        self._last_sent: float = 0.0  # monotonic timestamp of the last request slot

    @property
    def current_interval(self) -> float:
        with self._lock:
            return self._interval

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def wait(self) -> None:
        """Block until the next request slot is available, then claim it.

        The lock is held only while computing and reserving the deadline so
        concurrent callers queue up without sleeping under the lock.
        """
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._last_sent + self._interval - now)
            self._last_sent = now + sleep_for

        if sleep_for > 0.0:
            time.sleep(sleep_for)

    def success(self) -> None:
        """Additively decrease the inter-request interval (increase rate)."""
        with self._lock:
            self._interval = max(_MIN_INTERVAL, self._interval - _ADDITIVE_STEP)

    def throttled(self) -> None:
        """Multiplicatively increase the interval and persist a conservative value."""
        with self._lock:
            self._interval = min(_MAX_INTERVAL, self._interval * _BACKOFF_FACTOR)
            to_save = self._interval * 2.0
            domain = self.domain
        # Persist outside the lock to avoid holding it during I/O.
        _persist(domain, to_save)
