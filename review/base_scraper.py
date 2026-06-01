"""Abstract base class + result helpers for per-site scrapers."""
from abc import ABC, abstractmethod
import logging

from . import config

logger = logging.getLogger("review")


# Status constants
STATUS_DONE = "done"
STATUS_NOT_FOUND = "not_found"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"


def empty_result(status):
    return {"status": status, "rating": None, "reviews": []}


class SiteScraper(ABC):
    """Subclass per review site. Each subclass is registered automatically
    by importing it from review.sites.__init__.
    """
    SITE_ID: str = ""           # e.g. "yelp"
    DOMAIN: str = ""            # e.g. "yelp.com" — used by block_detect
    HAS_REVIEWS: bool = True    # False → emits only <site>_rating column
    MAX_REVIEWS: int = config.MAX_REVIEWS_PER_SITE
    MAX_RETRIES: int = config.DEFAULT_MAX_RETRIES
    USES_GOOGLE_DISCOVERY: bool = True  # use Google "site:foo.com {biz}" first
    # When True, the scraper will treat the site as a heavy blocker
    # (longer waits, more retries, more cautious behavior).
    HEAVY_BLOCKER: bool = False

    @abstractmethod
    def search(self, sb, row: dict):
        """Return profile/listing URL str, or None if business not found."""
        ...

    @abstractmethod
    def scrape(self, sb, profile_url: str) -> dict:
        """Return {'rating': float|None, 'reviews': [{author,rating,date,text},...]}."""
        ...

    def run(self, sb, row: dict) -> dict:
        """Wraps search→scrape with broad try/except. Returns standard result dict.

        scrape() can return:
          - {"rating": ..., "reviews": [...]}        → DONE if data, else NOT_FOUND
          - {"status": "blocked"} (or "error")        → propagated as-is
          - None                                       → STATUS_BLOCKED
                                                         (so the worker retries
                                                         with a fresh proxy IP)

        search() returning None is terminal-NOT_FOUND. To request a retry from
        the search step, raise an exception or return a sentinel via scrape()
        instead of returning None from search.
        """
        try:
            profile_url = self.search(sb, row)
        except Exception as e:
            logger.warning(f"[{self.SITE_ID}] search() raised: {type(e).__name__}: {e}")
            return empty_result(STATUS_ERROR)

        if not profile_url:
            # If the discovery layer (DDG) was likely just blocked/empty,
            # treat as BLOCKED so the worker retries with a fresh IP.
            # Heavy-blocker sites depend entirely on DDG search, so a NULL
            # search() result is more often "blocked discovery" than a real
            # absence. Light-blocker sites (DOMAIN owns its own search) treat
            # null search() as terminal not_found.
            if self.HEAVY_BLOCKER:
                return empty_result(STATUS_BLOCKED)
            return empty_result(STATUS_NOT_FOUND)

        try:
            data = self.scrape(sb, profile_url)
        except Exception as e:
            logger.warning(f"[{self.SITE_ID}] scrape() raised: {type(e).__name__}: {e}")
            return empty_result(STATUS_ERROR)

        if data is None:
            return empty_result(STATUS_BLOCKED)
        if isinstance(data, dict) and data.get("status") in (
            STATUS_BLOCKED, STATUS_ERROR, STATUS_NOT_FOUND,
        ):
            return empty_result(data["status"])

        data = data or {}
        rating = data.get("rating")
        reviews = data.get("reviews") or []
        if rating is None and not reviews:
            return empty_result(STATUS_NOT_FOUND)

        return {
            "status": STATUS_DONE,
            "rating": rating,
            "reviews": reviews[: self.MAX_REVIEWS] if isinstance(reviews, list) else [],
        }
