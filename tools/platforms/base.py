"""
base.py — Abstract base class for all platform slot fetchers.

Every platform fetcher inherits from BaseSlotFetcher and implements:
    fetch(city: dict, hours_ahead: int) -> list[dict]

The return value is a list of normalized slot dicts (via normalize_slot.normalize).
The base class provides:
    - shared requests.Session with browser-like headers + retry logic
    - _normalize() wrapper around normalize_slot.normalize
    - _within_window() helper
    - consistent logging prefix

Usage (standalone script mode):
    Each fetcher is also a standalone script with a main() function.
    Running it directly is equivalent to the old fetch_*.py tools.

Usage (programmatic):
    from tools.platforms.eventbrite import EventbriteFetcher
    fetcher = EventbriteFetcher()
    slots = fetcher.fetch({"city": "New York", "state": "NY", "lat": 40.71, "lng": -74.01}, 72)
"""

import sys
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, ".")
from tools.normalize_slot import normalize, is_within_window  # noqa: E402

# Browser-like headers shared across all scrapers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}


def _make_session(retries: int = 2, backoff: float = 0.5) -> requests.Session:
    """Create a requests.Session with retry logic and browser-like headers."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class BaseSlotFetcher(ABC):
    """
    Abstract base for all platform fetchers.

    Subclasses must define:
        PLATFORM_NAME: str          — e.g. "eventbrite"
        CATEGORY:      str          — default category; override per-slot if needed
        DATA_SOURCE:   str          — "api" | "scrape" | "ical"

    Subclasses must implement:
        fetch(city, hours_ahead) -> list[dict]
            city: {"city": str, "state": str, "lat": float, "lng": float}
            Return list of normalized slots (dicts from normalize_slot.normalize).

    Optional overrides:
        _make_session()     — customize HTTP client
        _request_delay()    — delay between requests; default 0s
    """

    PLATFORM_NAME: str = ""
    CATEGORY: str = "events"
    DATA_SOURCE: str = "scrape"

    def __init__(self, request_delay: float = 0.0):
        self._delay = request_delay
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = _make_session()
        return self._session

    @abstractmethod
    def fetch(self, city: dict, hours_ahead: int = 72) -> list[dict]:
        """
        Fetch open slots for a single city within hours_ahead hours.

        Args:
            city: {"city": str, "state": str, "lat": float, "lng": float}
            hours_ahead: look-ahead window in hours (default 72)

        Returns:
            List of normalized slot dicts. Empty list if nothing found or on error.
        """
        ...

    def fetch_all_cities(self, cities: list[dict], hours_ahead: int = 72) -> list[dict]:
        """
        Fetch slots for multiple cities sequentially with delay.
        Aggregates and deduplicates by slot_id.
        """
        seen: set[str] = set()
        results: list[dict] = []

        for i, city in enumerate(cities):
            tag = f"{city.get('city')}, {city.get('state')}"
            try:
                slots = self.fetch(city, hours_ahead)
                new_slots = [s for s in slots if s["slot_id"] not in seen]
                seen.update(s["slot_id"] for s in new_slots)
                results.extend(new_slots)
                if new_slots:
                    print(f"  [{self.PLATFORM_NAME}] {tag}: {len(new_slots)} new slots")
            except Exception as exc:
                print(f"  [{self.PLATFORM_NAME}] {tag}: ERROR — {exc}")

            if self._delay > 0 and i < len(cities) - 1:
                time.sleep(self._delay)

        return results

    def _normalize(self, raw: dict) -> dict:
        """Thin wrapper around normalize_slot.normalize for this platform."""
        return normalize(raw, self.PLATFORM_NAME)

    def _within_window(self, slot: dict, hours_ahead: float = 72.0) -> bool:
        return is_within_window(slot, hours_ahead)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(platform={self.PLATFORM_NAME!r})"
