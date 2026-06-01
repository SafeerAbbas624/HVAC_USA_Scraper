"""Anti-bot block detection (Cloudflare, PerimeterX, generic CAPTCHA)."""
import logging

from . import config

logger = logging.getLogger("review")


def is_blocked(sb, site_domain: str = "") -> bool:
    """Return True if the current page looks like a bot-block / CAPTCHA wall.

    Checks URL, title, and (truncated) page body for signal strings, plus
    a body-size sanity floor — sites under 1KB are almost always block stubs.
    """
    try:
        url = (sb.get_current_url() or "").lower()
        if "/sorry" in url or "captcha" in url or "challenge" in url:
            return True
        if site_domain and site_domain not in url and "google.com" not in url:
            # Redirected away from the target site → likely block redirect
            # (allow google.com because we use Google site-search for discovery)
            pass
    except Exception:
        pass

    try:
        title = (sb.get_title() or "").lower()
        for sig in config.BLOCK_SIGNAL_STRINGS:
            if sig in title:
                return True
    except Exception:
        pass

    try:
        body = (sb.get_page_source() or "")
        if len(body) < config.MIN_VALID_BODY_BYTES:
            return True
        body_low = body[:8000].lower()
        for sig in config.BLOCK_SIGNAL_STRINGS:
            if sig in body_low:
                return True
    except Exception:
        pass

    return False


def is_404_or_empty(sb) -> bool:
    """Light heuristic for 'page renders but business not present'."""
    try:
        title = (sb.get_title() or "").lower()
        if "404" in title or "not found" in title or "page not found" in title:
            return True
        body = (sb.get_page_source() or "")
        if len(body) < 500:
            return True
    except Exception:
        pass
    return False
