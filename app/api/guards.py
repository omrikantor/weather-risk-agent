"""Protection for a public deployment: access code, per-IP rate limit, daily OpenAI spend cap."""

import hmac
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone


def code_matches(given: str | None, expected: str) -> bool:
    """Compare a supplied code with the expected one in constant time, so response timing does not reveal how
    many leading characters were right."""
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


class RateLimiter:
    """Sliding one-minute window per key (client IP)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record a request and return False if this key already made per_minute requests in the last 60 s."""
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class DailyBudget:
    """Tracks OpenAI spend per UTC day. The cap can be changed at runtime (admin endpoint).

    The count lives in memory, so a restart resets it. That is acceptable here because the provider account
    also has its own spending limit; a multi-instance deployment would keep this count in a shared store.
    """

    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self._day = None
        self._spent = 0.0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        """Start a new count when the UTC date changes."""
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._day:
            self._day, self._spent = today, 0.0

    def available(self) -> bool:
        """True while today's spend is below the cap."""
        with self._lock:
            self._roll()
            return self._spent < self.cap_usd

    def add(self, cost: float) -> None:
        """Add the cost of a finished chat turn."""
        with self._lock:
            self._roll()
            self._spent += cost

    def status(self) -> dict:
        """Today's cap, spend and remaining budget (shown in /health)."""
        with self._lock:
            self._roll()
            return {"day_utc": self._day, "cap_usd": round(self.cap_usd, 2), "spent_usd": round(self._spent, 4),
                    "remaining_usd": round(max(self.cap_usd - self._spent, 0), 4)}
