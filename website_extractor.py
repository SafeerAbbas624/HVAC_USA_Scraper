"""
Website content extractor.

Visits individual websites to extract homepage HTML, contact page HTML,
and logo image URL using the requests library (fast, with reliable timeouts).
SeleniumBase is only needed for Google scraping (uc=True anti-bot bypass).
"""
import re
import logging
import warnings
from urllib.parse import urlparse, urljoin

import requests
import urllib3

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS")
logger = logging.getLogger("scraper")

# Typical browser User-Agent so websites don't reject us
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _build_session() -> requests.Session:
    """Build a requests.Session with headers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    s.verify = False  # some biz sites have bad certs
    return s


def _fetch_page(session: requests.Session, url: str, timeout: int) -> str:
    """GET a URL and return its text content, or '' on any error."""
    try:
        resp = session.get(url, timeout=(10, timeout), allow_redirects=True)
        if resp.status_code < 400:
            return resp.text[:config.MAX_RAW_HTML_LENGTH]
    except Exception:
        pass
    return ""


def extract_logo_url(html: str, base_url: str) -> str:
    """
    Extract logo image URL from HTML source using regex patterns.
    """
    # Patterns ordered by specificity
    patterns = [
        r'<img[^>]+src=["\']([^"\']+logo[^"\']*)["\']',
        r'<img[^>]+class=["\'][^"\']*logo[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+alt=["\'][^"\']*logo[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+id=["\'][^"\']*logo[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            src = m.group(1)
            if not src.startswith("http"):
                src = urljoin(base_url, src)
            return src
    return ""


def try_contact_page(session: requests.Session, base_url: str, timeout: int) -> str:
    """
    Try common contact page paths and return the first valid one.
    """
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in config.CONTACT_PATHS:
        contact_url = base + path
        try:
            resp = session.get(
                contact_url, timeout=(10, timeout), allow_redirects=True
            )
            if resp.status_code < 400 and len(resp.text) > 500:
                # Skip 404-style pages
                if "404" not in resp.text[:500].lower() and "not found" not in resp.text[:500].lower():
                    logger.info(f"Found contact page: {contact_url}")
                    return resp.text[:config.MAX_RAW_HTML_LENGTH]
        except Exception:
            continue
    return ""


def extract_website_content(url: str) -> dict:
    """
    Visit a website and extract homepage HTML, contact page HTML, and logo URL.

    Uses the requests library with reliable timeouts instead of SeleniumBase.
    """
    result = {
        "homepage_html": "",
        "contact_html": "",
        "logo_url": "",
    }

    timeout = config.WEBSITE_VISIT_TIMEOUT  # per-request timeout

    session = _build_session()

    # ---------- Homepage ----------
    logger.info(f"Visiting website: {url}")
    homepage_html = _fetch_page(session, url, timeout)
    if homepage_html:
        result["homepage_html"] = homepage_html

    # ---------- Logo ----------
    if homepage_html:
        result["logo_url"] = extract_logo_url(homepage_html, url)

    # ---------- Contact page ----------
    result["contact_html"] = try_contact_page(session, url, timeout)

    session.close()
    return result

