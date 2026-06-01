"""HomeAdvisor (homeadvisor.com).

Verified workflow (2026-04-29):
  HA's old /c.{name}.{city}.{state}.html search format is dead — it now
  returns Page Not Found. The contractor profile URL pattern is
  /rated.<slug>.<id>.html. Discovery: DuckDuckGo.

  Profile pages have full LocalBusiness JSON-LD with aggregateRating +
  review[] including author, datePublished, reviewBody, reviewRating.
"""
import logging
import re

from ..base_scraper import SiteScraper
from . import _common as c

logger = logging.getLogger("review")


class HomeAdvisorReviews(SiteScraper):
    SITE_ID = "homeadvisor"
    DOMAIN = "homeadvisor.com"
    HAS_REVIEWS = True
    # Most HVAC businesses genuinely don't have HA profiles — a null search()
    # result is usually correct, not a transient block. Keep HEAVY_BLOCKER
    # off so we don't spend 5 retry slots on every absent business.
    # The DDG search itself retries internally (3 attempts) to handle SERP
    # rate-limiting before declaring the profile absent.

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
            sb, "homeadvisor.com", name, city, state,
            path_regex=r"homeadvisor\.com/rated\.",
            max_results=8,
        )
        # Pick the first /rated/ URL — it's a contractor profile
        for cand in cands:
            if "/rated." in cand:
                return cand.split("?")[0]
        return None

    def scrape(self, sb, profile_url):
        html = c.cdp_open(sb, profile_url, settle_seconds=(6, 9))
        if not html or c.cdp_is_blocked(html):
            return None  # → BLOCKED, retry with fresh proxy

        rating, reviews = c.jsonld_extract_rating_and_reviews(
            html, max_reviews=self.MAX_REVIEWS
        )
        if rating is None:
            m = re.search(
                r'"ratingValue"\s*:\s*"?(\d(?:\.\d{1,4})?)', html
            )
            if m:
                try:
                    rating = round(float(m.group(1)), 2)
                except Exception:
                    pass

        # Discovered business name from LocalBusiness JSON-LD.
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
