"""Yelp scraper.

Yelp's edge layer (DataDome) rejects all datacenter / shared-proxy IPs
with a 403 — even on /robots.txt — so direct scraping with Selenium or
curl_cffi can't work from cloud infrastructure. Verified on 2026-04-29
across 30+ attempts (UC mode, CDP mode, 5 browser TLS impersonations,
api.yelp.com, m.yelp.com, /robots.txt, Wayback Machine, SERP scraping,
Google Maps panel — all 403 / no data).

Production strategy (3 layers, fastest first):

  LAYER 1 → Yelp Academic Open Dataset (offline lookup, free, no rate limit)
            ~160K businesses + ~7M reviews from MA/OR/TX/FL/GA/BC/OH/CO/WA.
            Coverage is regional, but matches we DO get are 100% reliable
            and include up to 100 reviews per business. See
            review/yelp_dataset_loader.py for the loader.
            Files (download from archive.org once):
              review/yelp_dataset/yelp_academic_dataset_business.json.gz
              review/yelp_dataset/yelp_academic_dataset_review.json.gz

  LAYER 2 → Yelp Fusion API (https://docs.developer.yelp.com/)
            Free 5000 calls/day. Requires YELP_API_KEY env var.
            Returns rating + review_count + up to 3 reviews per business.
            Sign up: https://www.yelp.com/developers/v3/manage_app

  LAYER 3 → NOT_FOUND (no dataset match + no API key) — terminal, fast.
"""
import logging
import os
import time

from curl_cffi import requests as cf_requests

from .. import config
from .. import yelp_dataset_loader as yd
from ..base_scraper import SiteScraper
from . import _common as c

logger = logging.getLogger("review")

YELP_API_KEY = os.environ.get("YELP_API_KEY", "").strip()

_API_NOTICE_LOGGED = False
_DATASET_NOTICE_LOGGED = False


def _maybe_log_no_key_notice():
    global _API_NOTICE_LOGGED
    if _API_NOTICE_LOGGED:
        return
    _API_NOTICE_LOGGED = True
    logger.warning(
        "[yelp] YELP_API_KEY not set — Yelp scraping will return NOT_FOUND "
        "for every row. Get a free key at "
        "https://www.yelp.com/developers/v3/manage_app (5000 calls/day) "
        "and add YELP_API_KEY=... to /var/HVAC/.env to enable Yelp."
    )


def _maybe_log_no_dataset_notice():
    global _DATASET_NOTICE_LOGGED
    if _DATASET_NOTICE_LOGGED:
        return
    _DATASET_NOTICE_LOGGED = True
    logger.warning(
        "[yelp] No Yelp dataset and no YELP_API_KEY — Yelp will return "
        "NOT_FOUND for every row. To enable: download the academic dataset "
        "from archive.org/details/yelp_academic_dataset_review.json "
        "to /var/HVAC/review/yelp_dataset/ and run "
        "`python -m review.yelp_dataset_loader build hvac_companies_cleaned.csv`. "
        "Or set YELP_API_KEY in /var/HVAC/.env."
    )


class YelpReviews(SiteScraper):
    SITE_ID = "yelp"
    DOMAIN = "yelp.com"
    HAS_REVIEWS = True
    # No Selenium / no network retries needed — dataset lookup is offline,
    # API path is synchronous. Two attempts at most for transient API errors.
    HEAVY_BLOCKER = False
    MAX_RETRIES = 2

    def search(self, sb, row):
        """Stash the row on this instance and return a sentinel URL.

        The real work happens in scrape(); we just need search() to return
        non-None so base_scraper.run() proceeds to scrape().

        Empty-name fallback: if input has no business_name but does have
        a website_url, we'll use the domain apex (e.g. acmehvac.com →
        "acmehvac") as the search term in scrape().
        """
        name = (row.get("business_name") or "").strip()
        if not name:
            apex = c.domain_apex(row.get("website_url", ""))
            if not apex:
                return None
        # Per-subprocess state (each scrape attempt is its own process).
        self._row = row
        return "yelp://lookup"

    def scrape(self, sb, profile_url):
        row = getattr(self, "_row", None) or {}
        name = (row.get("business_name") or "").strip()
        city = row.get("business_city", "")
        state = row.get("business_state", "")

        # Empty-name fallback: use website's domain apex as the lookup name.
        # The Yelp Open Dataset is keyed by name, so a domain-derived name
        # can still hit if the listing's name happens to match (often does
        # for small businesses whose Yelp listing is named after their site).
        if not name:
            name = c.domain_apex(row.get("website_url", ""))
            if not name:
                return {"rating": None, "reviews": []}
            logger.info(f"[yelp] using domain-apex fallback name: {name!r}")

        # Layer 1a: pre-built matches cache (O(1), ~MB to load)
        data = yd.get_yelp_data_fast(name, city, state)

        # Layer 1b: full dataset lookup (heavier ~5s first call per process)
        if data is None and yd.is_available():
            data = yd.get_yelp_data(name, city, state)

        if data and data.get("rating") is not None:
            logger.info(
                f"[yelp] dataset hit: {data.get('matched_name')!r} "
                f"stars={data['rating']} n={data.get('review_count')} "
                f"reviews_cached={len(data.get('reviews', []))}"
            )
            return {
                "rating": data["rating"],
                "reviews": data.get("reviews", [])[: self.MAX_REVIEWS],
                "business_name": data.get("matched_name") or None,
            }

        # Layer 2: Yelp Fusion API (only if a key is configured)
        if YELP_API_KEY:
            # Stash the synthetic name back on row so _scrape_via_api uses it.
            api_row = dict(row)
            api_row["business_name"] = name
            return _scrape_via_api(api_row, max_reviews=self.MAX_REVIEWS)

        # Layer 3: NOT_FOUND
        if not yd.is_available():
            _maybe_log_no_dataset_notice()
        return {"rating": None, "reviews": []}


# ---------------------------------------------------------------- API client

_API_BASE = "https://api.yelp.com/v3"


def _api_get(path, params=None, headers=None):
    """Issue a GET to api.yelp.com using curl_cffi (no Selenium needed)."""
    url = f"{_API_BASE}{path}"
    h = {"Authorization": f"Bearer {YELP_API_KEY}", "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        r = cf_requests.get(
            url,
            headers=h,
            params=params or {},
            impersonate="chrome124",
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
        logger.debug(f"[yelp] API {path} → status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        logger.debug(f"[yelp] API {path} raised: {type(e).__name__}: {e}")
    return None


def _scrape_via_api(row, max_reviews=100):
    """Look up a business via Yelp Fusion API and return rating + reviews."""
    if not YELP_API_KEY:
        _maybe_log_no_key_notice()
        # Return None=blocked? No — terminal not_found is correct: there's no
        # path to fix this attempt-by-attempt. Caller treats NOT_FOUND as
        # terminal which is what we want.
        return {"rating": None, "reviews": []}

    name = (row.get("business_name") or "").strip()
    address = (row.get("address") or "").strip()
    city = (row.get("business_city") or "").strip()
    state = (row.get("business_state") or "").strip()
    if not name:
        return {"rating": None, "reviews": []}

    # Yelp's `/businesses/matches` endpoint maps name+address to a Yelp ID.
    # Required: name, address1, city, state, country. address1 can be empty.
    address1 = ""
    if address:
        # Address has format "<street>, <city>, <state> <zip>" — pull street
        parts = address.split(",")
        address1 = parts[0].strip()[:80]

    matches = _api_get("/businesses/matches", params={
        "name": name[:64],
        "address1": address1[:80],
        "city": city[:40],
        "state": state[:3],
        "country": "US",
        "limit": 3,
    })
    if not matches or "businesses" not in matches:
        return {"rating": None, "reviews": []}
    businesses = matches.get("businesses") or []
    if not businesses:
        return {"rating": None, "reviews": []}

    # Take the first match (Yelp ranks by relevance)
    biz_id = businesses[0].get("id")
    if not biz_id:
        return {"rating": None, "reviews": []}

    # Get the rating + review_count
    biz = _api_get(f"/businesses/{biz_id}")
    if not biz:
        return {"rating": None, "reviews": []}
    rating = biz.get("rating")
    if rating is not None:
        try:
            rating = float(rating)
        except Exception:
            rating = None
    discovered_name = (biz.get("name") or "").strip() or None

    # Get up to 3 reviews (API limit on Fusion free tier)
    reviews_resp = _api_get(f"/businesses/{biz_id}/reviews",
                             params={"limit": min(max_reviews, 50)})
    reviews = []
    if reviews_resp and reviews_resp.get("reviews"):
        for r in reviews_resp["reviews"][:max_reviews]:
            user = r.get("user") or {}
            reviews.append({
                "author": (user.get("name") or "").strip(),
                "rating": r.get("rating"),
                "date": (r.get("time_created") or "")[:10],
                "text": (r.get("text") or "").strip(),
            })

    return {"rating": rating, "reviews": reviews, "business_name": discovered_name}
