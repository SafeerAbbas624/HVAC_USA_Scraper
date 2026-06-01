"""Thin re-export of the existing proxy loader from /var/HVAC/google_scraper.py.

Adds a thread-safe rotation helper for the per-site retry loop so two threads
don't simultaneously pick the same proxy on retry.
"""
import logging
import random
import sys
import os
import threading

# Ensure parent project dir is importable so we can reuse google_scraper helpers
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from google_scraper import load_proxies as _load_proxies  # noqa: E402

from . import config

logger = logging.getLogger("review")

_lock = threading.Lock()


def load_proxies():
    """Load proxies from /var/HVAC/proxies.txt."""
    proxies = _load_proxies(config.PROXIES_FILE)
    if not proxies:
        logger.warning(
            "No proxies loaded — sites will be hit from the local IP and "
            "will likely block within minutes. Configure proxies.txt."
        )
    return proxies


def pick_proxy(proxies, exclude=None):
    """Pick a random proxy not in `exclude` (a set of proxy ids).

    Falls back to ANY proxy if all have been excluded — better to retry with
    a previously-used proxy than to skip the site entirely.
    """
    if not proxies:
        return None
    exclude = exclude or set()
    with _lock:
        candidates = [p for p in proxies if proxy_id(p) not in exclude]
        if not candidates:
            candidates = proxies
        return random.choice(candidates)


def proxy_id(proxy):
    """Stable hash for a proxy dict — used as a 'used proxies' key."""
    if not proxy:
        return ""
    return f"{proxy.get('scheme','http')}://{proxy.get('ip','')}:{proxy.get('port','')}"
