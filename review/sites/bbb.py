"""Better Business Bureau (bbb.org) — letter-grade rating only.

Verified workflow (2026-04-29):
  1. Open bbb.org/search?find_text=NAME&find_loc=CITY,STATE via CDP mode.
  2. Walk a[href] for /us/<state>/<city>/profile/<cat>/<slug>-<id>.
  3. Open the profile via CDP. Letter grade is in span.bpr-letter-grade,
     fallback span.bpr-not-rated. Map letter→numeric 0-5.
"""
import logging
import re
from urllib.parse import quote_plus

from .. import config
from ..base_scraper import SiteScraper
from . import _common as c

logger = logging.getLogger("review")


class BBBRating(SiteScraper):
    SITE_ID = "bbb"
    DOMAIN = "bbb.org"
    HAS_REVIEWS = False
    HEAVY_BLOCKER = True
    MAX_RETRIES = config.HEAVY_BLOCKER_RETRIES

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()

        # Empty-name fallback: use the website's domain apex (e.g. acmehvac.com
        # → "acmehvac") as the search term. Better than nothing — works for
        # most small businesses whose Yelp/BBB listing is named after their
        # domain.
        if not name:
            apex = c.domain_apex(row.get("website_url", ""))
            if not apex:
                return None
            name = apex

        # Direct BBB search
        url = (f"https://www.bbb.org/search?"
               f"find_text={quote_plus(name)}"
               f"&find_loc={quote_plus(f'{city}, {state}')}")
        html = c.cdp_open(sb, url, settle_seconds=(5, 7))
        if html and not c.cdp_is_blocked(html):
            hrefs = c.cdp_eval(
                sb,
                "Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.getAttribute('href'))"
                ".filter(h => h && /\\/profile\\//.test(h))"
                ".slice(0, 12)"
            ) or []
            for h in hrefs:
                if not h:
                    continue
                if re.match(r"^/(us|ca)/[a-z]{2,3}/[\w-]+/profile/[\w-]+/", h, re.I):
                    return "https://www.bbb.org" + h.split("?")[0]

        # DDG fallback
        cands = c.find_profile_url(
            sb, "bbb.org", name, city, state,
            path_regex=r"bbb\.org/(us|ca)/[a-z]{2,3}/[\w-]+/profile/",
            max_results=5,
        )
        if cands:
            return cands[0]
        return None

    def scrape(self, sb, profile_url):
        html = c.cdp_open(sb, profile_url, settle_seconds=(5, 7))
        if not html or c.cdp_is_blocked(html):
            return None  # → BLOCKED, retry with fresh proxy

        # Letter grade
        letter = None
        m = re.search(
            r'class="bpr-letter-grade"[^>]*>([A-F][+\-]?)<', html
        )
        if m:
            letter = m.group(1)
        else:
            # Fallback: header rating slot
            m2 = re.search(
                r'class="bpr-header-rating"[^>]*>([A-F][+\-]?)<', html
            )
            if m2:
                letter = m2.group(1)
        rating = _letter_to_rating(letter) if letter else None

        # Discovered business name from the BBB profile (used to backfill
        # rows whose input had no business_name).
        # Verified selector: <span id="businessName" class="bpr-header-business-name">
        discovered_name = None
        nm = (
            re.search(r'id="businessName"[^>]*>\s*([^<]+?)\s*<', html)
            or re.search(r'class="[^"]*bpr-header-business-name[^"]*"[^>]*>\s*([^<]+?)\s*<', html)
        )
        if nm:
            t = re.sub(r"\s+", " ", nm.group(1)).strip()
            t = (t.replace("&amp;", "&")
                  .replace("&#39;", "'")
                  .replace("&quot;", '"'))
            if t and len(t) < 200 and t.lower() != "about":
                discovered_name = t

        # If "Not Rated", treat as no rating (will become not_found upstream)
        return {
            "rating": rating,
            "reviews": [],
            "business_name": discovered_name,
        }


_LETTER_MAP = {
    "A+": 5.0, "A": 4.7, "A-": 4.4,
    "B+": 4.1, "B": 3.8, "B-": 3.5,
    "C+": 3.2, "C": 2.9, "C-": 2.6,
    "D+": 2.3, "D": 2.0, "D-": 1.7,
    "F": 1.0,
}


def _letter_to_rating(letter):
    return _LETTER_MAP.get(letter)
