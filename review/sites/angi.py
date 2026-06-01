"""Angi (formerly Angie's List) — angi.com.

Verified workflow (2026-04-29):
  Angi's /search results page obfuscates business names ("pseudo text") for
  unauthenticated visitors, so we can't rely on it. Instead:
  1. Find the canonical /companylist/us/{state}/{city}/{slug}-reviews-{id}.htm
     URL via DuckDuckGo HTML search. Profile pages have full data.
  2. Open the profile via CDP mode. Cloudflare blocks ~30% of requests on
     this path; the worker retry layer rotates IPs.
  3. Extract rating + reviews from the LocalBusiness JSON-LD block on the
     page (always present, much more stable than CSS selectors).
"""
import logging
import re

from .. import config
from ..base_scraper import SiteScraper
from . import _common as c

logger = logging.getLogger("review")


class AngiReviews(SiteScraper):
    SITE_ID = "angi"
    DOMAIN = "angi.com"
    HAS_REVIEWS = True
    HEAVY_BLOCKER = True
    MAX_RETRIES = config.HEAVY_BLOCKER_RETRIES

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()

        # Empty-name fallback: use website's domain apex as the search term.
        if not name:
            apex = c.domain_apex(row.get("website_url", ""))
            if not apex:
                return None
            name = apex

        cands = c.find_profile_url(
            sb, "angi.com", name, city, state,
            path_regex=r"angi\.com/companylist/us/[a-z]{2}/[\w-]+/[\w-]+",
            max_results=5,
        )
        for cand in cands:
            return cand.split("?")[0]
        return None

    def scrape(self, sb, profile_url):
        html = c.cdp_open(sb, profile_url, settle_seconds=(6, 9))
        if not html or c.cdp_is_blocked(html):
            return None  # → BLOCKED, retry with fresh proxy
        # Next.js error page (e.g. URL no longer valid) → terminal not-found
        if '<html id="__next_error__"' in html or "Not Found" in html[:2000]:
            return {"rating": None, "reviews": []}

        rating, reviews = c.jsonld_extract_rating_and_reviews(
            html, max_reviews=self.MAX_REVIEWS
        )
        # Some profile pages render rating in HTML but lack JSON-LD reviews.
        # Selector fallback for rating only:
        if rating is None:
            m = re.search(
                r'class="RatingsLockup_ratingNumber[^"]*"[^>]*>(\d(?:\.\d)?)<',
                html,
            )
            if m:
                try:
                    rating = float(m.group(1))
                except Exception:
                    pass
        if rating is None:
            m = re.search(
                r'"ratingValue"\s*:\s*"?(\d(?:\.\d{1,3})?)', html
            )
            if m:
                try:
                    rating = round(float(m.group(1)), 2)
                except Exception:
                    pass

        # Discovered business name from the LocalBusiness JSON-LD.
        biz = c.jsonld_local_business(html)
        discovered_name = (biz.get("name") or "").strip() if biz else None
        if discovered_name:
            discovered_name = (
                discovered_name.replace("&amp;", "&")
                               .replace("&#39;", "'")
                               .replace("&quot;", '"')
            )

        return {
            "rating": rating,
            "reviews": reviews,
            "business_name": discovered_name,
        }
