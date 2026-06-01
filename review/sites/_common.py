"""Shared helpers used by per-site scrapers.

Discovery: DuckDuckGo HTML search (Google site-search is heavily rate-limited
on shared proxies; DDG tolerates more).

Page open: SB CDP Mode (sb.activate_cdp_mode + sb.cdp.open) — most undetectable
bypass for DataDome / Cloudflare / PerimeterX.
"""
import logging
import json
import random
import re
import time
from urllib.parse import urlparse, quote_plus, unquote

from .. import block_detect

logger = logging.getLogger("review")


# ---------------------------------------------------------------- URL helpers

def domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        d = urlparse(url if "://" in url else "http://" + url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


_COMPOUND_TLDS = frozenset({
    "co.uk", "co.nz", "co.za", "co.in", "co.jp", "co.kr",
    "com.au", "com.br", "com.mx", "com.cn", "com.tr", "com.sg",
    "net.au", "org.uk", "ac.uk",
})


def domain_apex(url: str) -> str:
    """Return the second-level-domain (without TLD) as a search-friendly token.

    Examples:
        https://www.acmehvac.com/         → 'acmehvac'
        airpros-energy.net                → 'airpros-energy'
        sub.example.co.uk                 → 'example'
        ''                                → ''
    Used as a fallback "name" when the input row has no business_name.
    """
    d = domain_from_url(url)
    if not d:
        return ""
    parts = d.split(".")
    # Compound TLD case (.co.uk etc.) — take the part before the compound.
    if len(parts) >= 3 and ".".join(parts[-2:]) in _COMPOUND_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s


# ---------------------------------------------------------------- CDP page ops

def cdp_open(sb, url: str, settle_seconds=(4, 6)) -> str:
    """Open `url` via CDP mode and return page source. Empty string on error.

    First call activates CDP mode (sets up sb.cdp). Subsequent calls reuse
    the open driver via sb.cdp.open.
    """
    try:
        if not getattr(sb, "cdp", None):
            sb.activate_cdp_mode(url)
        else:
            sb.cdp.open(url)
        time.sleep(random.uniform(*settle_seconds))
        return sb.cdp.get_page_source() or ""
    except Exception as e:
        logger.debug(f"cdp_open({url}): {type(e).__name__}: {e}")
        return ""


def cdp_eval(sb, js: str):
    """Run JS via CDP and return the result, or None on error."""
    try:
        return sb.cdp.evaluate(js)
    except Exception as e:
        logger.debug(f"cdp_eval failed: {e}")
        return None


def cdp_scroll(sb, n_times=8, pause=(0.6, 1.2)):
    """Scroll-to-bottom via CDP — triggers lazy-load on most review sites."""
    for _ in range(n_times):
        try:
            sb.cdp.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            break
        time.sleep(random.uniform(*pause))


# ---------------------------------------------------------------- Discovery

def _ddg_extract(html: str) -> list:
    out = []
    seen = set()
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        candidate = m.group(1)
        if "duckduckgo.com" in candidate or "duck.co" in candidate:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    for m in re.finditer(r'href="(?://duckduckgo\.com)?/l/\?uddg=([^"&]+)', html):
        try:
            decoded = unquote(m.group(1))
            if decoded.startswith("//"):
                decoded = "https:" + decoded
            if decoded not in seen:
                seen.add(decoded)
                out.append(decoded)
        except Exception:
            continue
    return out


def ddg_html_search(sb, query: str, retries: int = 2) -> list:
    """Search DuckDuckGo HTML version and return all result URLs (decoded).

    DDG sometimes returns a near-empty page or rate-limits — retry up to
    `retries` times before giving up. We also fall through to Bing if DDG
    yields nothing across retries.
    """
    last_html = ""
    for attempt in range(retries):
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        html = cdp_open(sb, url, settle_seconds=(3, 5) if attempt == 0 else (5, 8))
        last_html = html or last_html
        if not html or len(html) < 5000:
            time.sleep(random.uniform(2, 4))
            continue
        results = _ddg_extract(html)
        if results:
            return results
        time.sleep(random.uniform(2, 4))
    # Bing fallback (similar SERP structure; tolerates a different IP pool)
    return bing_search(sb, query) or (_ddg_extract(last_html) if last_html else [])


def bing_search(sb, query: str) -> list:
    """Search Bing and return all result URLs."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    html = cdp_open(sb, url, settle_seconds=(3, 5))
    if not html or len(html) < 5000:
        return []
    out = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"', html):
        candidate = m.group(1)
        if "bing.com" in candidate or "microsoft.com" in candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def _strip_for_search(name: str) -> str:
    """Drop punctuation that confuses DDG's exact match (&, commas, LLC suffix).

    Preserves word order. DDG indexes on tokens, not bag-of-words.
    """
    s = re.sub(r"[&]", "and", name or "")
    s = re.sub(r"[,.\"'\(\)]", " ", s)
    # Drop legal suffixes that often don't appear on the listing page
    s = re.sub(r"\b(LLC|Inc|Corp|Ltd|Co)\b\.?", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_profile_url(sb, target_domain: str, name: str, city: str = "",
                     state: str = "", path_regex: str = None,
                     max_results: int = 8) -> list:
    """Find candidate profile URLs on target_domain via DuckDuckGo.

    Tries two queries: a stricter quoted match first, then a token-only
    fallback. Returns matching URLs in priority order.
    """
    target_low = target_domain.lower()
    clean_name = _strip_for_search(name)

    # Build a list of queries from most-specific to least-specific
    queries = []
    if clean_name and city and state:
        queries.append(f'site:{target_domain} "{clean_name}" {city} {state}')
    if clean_name and city:
        queries.append(f"site:{target_domain} {clean_name} {city} {state}".strip())
    if clean_name:
        queries.append(f"site:{target_domain} {clean_name}")

    out = []
    seen = set()
    for q in queries:
        candidates = ddg_html_search(sb, q)
        for href in candidates:
            try:
                host = urlparse(href).netloc.lower()
            except Exception:
                continue
            if target_low not in host:
                continue
            clean = href.split("#")[0]
            if path_regex and not re.search(path_regex, clean):
                continue
            if clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
            if len(out) >= max_results:
                return out
        if out:
            return out  # First query that yielded matches wins
    return out


# ---------------------------------------------------------------- JSON-LD

def extract_jsonld_blocks(html: str) -> list:
    """Return parsed JSON-LD blocks from a page (mostly LocalBusiness)."""
    out = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([^<]+)</script>',
        html, re.I | re.S
    ):
        raw = m.group(1).strip()
        try:
            parsed = json.loads(raw)
            out.append(parsed)
        except Exception:
            # Some sites embed multiple JSON objects in one script tag
            try:
                parsed = json.loads("[" + raw.replace("}\n{", "},\n{") + "]")
                if isinstance(parsed, list):
                    out.extend(parsed)
            except Exception:
                continue
    return out


def jsonld_local_business(html: str) -> dict:
    """Find the first LocalBusiness JSON-LD block (or @graph entry)."""
    for blk in extract_jsonld_blocks(html):
        if isinstance(blk, list):
            for sub in blk:
                if isinstance(sub, dict) and "LocalBusiness" in str(sub.get("@type", "")):
                    return sub
        elif isinstance(blk, dict):
            t = str(blk.get("@type", ""))
            if "LocalBusiness" in t or "Organization" in t or "ProfessionalService" in t:
                return blk
            if "@graph" in blk and isinstance(blk["@graph"], list):
                for sub in blk["@graph"]:
                    if isinstance(sub, dict) and "LocalBusiness" in str(sub.get("@type", "")):
                        return sub
    return {}


def jsonld_extract_rating_and_reviews(html: str, max_reviews: int = 100):
    """Pull rating + reviews from a page's LocalBusiness JSON-LD.

    Returns (rating: float|None, reviews: list[dict]).
    """
    biz = jsonld_local_business(html)
    if not biz:
        return None, []

    rating = None
    agg = biz.get("aggregateRating") or {}
    if isinstance(agg, dict):
        try:
            v = agg.get("ratingValue")
            if v is not None:
                rating = round(float(v), 2)
        except Exception:
            pass

    reviews = []
    raw_reviews = biz.get("review") or []
    if isinstance(raw_reviews, dict):
        raw_reviews = [raw_reviews]
    for r in raw_reviews[:max_reviews]:
        if not isinstance(r, dict):
            continue
        author = ""
        a = r.get("author")
        if isinstance(a, dict):
            author = a.get("name", "") or ""
        elif isinstance(a, str):
            author = a
        rv = None
        rr = r.get("reviewRating") or {}
        if isinstance(rr, dict):
            try:
                rv = float(rr.get("ratingValue")) if rr.get("ratingValue") is not None else None
            except Exception:
                rv = None
        text = r.get("reviewBody") or r.get("description") or ""
        date = r.get("datePublished") or ""
        # HTML-decode common entities
        if isinstance(text, str):
            text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        reviews.append({
            "author": (author or "").strip(),
            "rating": rv,
            "date": (date or "").strip()[:32],
            "text": (text or "").strip(),
        })
    return rating, reviews


# ---------------------------------------------------------------- Block check

def cdp_is_blocked(html: str) -> bool:
    """Cheap block heuristic for CDP-fetched HTML.

    DataDome / DD captcha pages are tiny (<5kb). Cloudflare interstitial says
    "Sorry, you have been blocked". Chrome's HTTP-403 error page contains
    "ERR_HTTP_RESPONSE_CODE_FAILURE" or "errorCode":"HTTP ERROR 403".
    """
    if not html:
        return True
    if len(html) < 5000:
        return True
    low = html[:6000].lower()
    if "sorry, you have been blocked" in low:
        return True
    if "error 1015" in low or "ray id" in low and "cloudflare" in low:
        return True
    if "datadome" in low and ("captcha-delivery" in low or "data-cfasync" in low):
        return True
    if "geo.captcha-delivery" in low:
        return True
    if '"errorCode":"http error 4' in html.lower():
        return True
    if "px-captcha" in low or "perimeterx" in low:
        return True
    return False


# ---------------------------------------------------------------- Misc

def name_match(candidate_name: str, expected_name: str) -> float:
    """Cheap fuzzy score 0..1 — token overlap."""
    if not candidate_name or not expected_name:
        return 0.0
    a = set(re.findall(r"\w+", candidate_name.lower()))
    b = set(re.findall(r"\w+", expected_name.lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


# ---------------------------------------------------------------- Selenium-mode helpers (kept for google_maps)

def safe_get(sb, url: str, settle_seconds=(2.5, 4.5)) -> bool:
    """Open url with reconnect; return False if the page is clearly blocked.

    Used by the google_maps scraper which still relies on classic UC mode
    (rich tab/click DOM interactions) instead of CDP mode.
    """
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=random.uniform(4, 7))
    except Exception:
        try:
            sb.open(url)
        except Exception:
            return False
    time.sleep(random.uniform(*settle_seconds))
    return not block_detect.is_blocked(sb)


def scroll_down(sb, n_times=8, pause=(0.6, 1.2)):
    for _ in range(n_times):
        try:
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            break
        time.sleep(random.uniform(*pause))


def click_load_more(sb, selectors, max_clicks=20):
    clicks = 0
    for _ in range(max_clicks):
        clicked = False
        for sel in selectors:
            try:
                btns = sb.find_elements("css selector", sel)
                for b in btns:
                    if b.is_displayed():
                        try:
                            b.click()
                            clicked = True
                            clicks += 1
                            time.sleep(random.uniform(1.0, 2.0))
                            break
                        except Exception:
                            continue
                if clicked:
                    break
            except Exception:
                continue
        if not clicked:
            break
    return clicks


def is_no_reviews_page(sb) -> bool:
    try:
        body = sb.get_page_source() or ""
    except Exception:
        return False
    return is_no_reviews_html(body)


def text_or_none(elem):
    try:
        s = elem.text.strip()
        return s if s else None
    except Exception:
        return None


def attr_or_none(elem, name):
    try:
        v = elem.get_attribute(name)
        return v if v else None
    except Exception:
        return None


_NO_REVIEWS_SIGNALS = (
    "no reviews yet",
    "be the first to review",
    "be the first to write a review",
    "no ratings yet",
    "not yet rated",
    "no reviews to display",
    "this business has not yet been reviewed",
    "haven't been reviewed yet",
    "we don't have any reviews",
)


def is_no_reviews_html(html: str) -> bool:
    if not html:
        return False
    low = html[:30000].lower()
    return any(sig in low for sig in _NO_REVIEWS_SIGNALS)


# Strict + bare rating regexes (kept for selector-based extraction)
_RATING_RX_STRICT = re.compile(
    r"(\d(?:\.\d{1,2})?)\s*(?:/\s*5|out of 5|stars?\b|★)", re.I
)
_RATING_RX_BARE = re.compile(r"^\s*(\d(?:\.\d{1,2})?)\b")


def extract_first_rating(text: str):
    if not text:
        return None
    for m in _RATING_RX_STRICT.finditer(text):
        try:
            v = float(m.group(1))
            if 0.0 < v <= 5.0:
                return v
        except ValueError:
            continue
    return None


def extract_rating_from_element_text(text: str):
    if not text:
        return None
    strict = extract_first_rating(text)
    if strict is not None:
        return strict
    text = text.strip()
    if not text or len(text) > 30:
        return None
    m = _RATING_RX_BARE.match(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        if 0.5 <= v <= 5.0:
            return v
    except ValueError:
        pass
    return None
