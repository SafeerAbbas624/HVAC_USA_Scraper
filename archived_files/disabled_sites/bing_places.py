"""Bing Maps / Bing Places — rating only (review text not exposed publicly)."""
import re
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class BingPlacesRating(SiteScraper):
    SITE_ID = "bing_places"
    DOMAIN = "bing.com"
    HAS_REVIEWS = False

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()
        url = f"https://www.bing.com/maps?q={quote_plus(f'{name} {city} {state}')}"
        if not c.safe_get(sb, url, settle_seconds=(3, 5)):
            return None
        return sb.get_current_url()

    def scrape(self, sb, profile_url):
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        # Bing Places shows the rating in a side panel
        for sel in ("div.b_subModuleRtg span", "div.b_factrow span.csrc",
                    "div[class*='rating']", "[aria-label*='star']"):
            try:
                els = sb.find_elements("css selector", sel)
                if els:
                    rating = c.extract_rating_from_element_text(
                        els[0].get_attribute("aria-label") or els[0].text)
                    if rating: break
            except Exception: continue
        if rating is None:
            try: rating = c.extract_first_rating(sb.get_text("body")[:5000])
            except Exception: pass
        return {"rating": rating, "reviews": []}
