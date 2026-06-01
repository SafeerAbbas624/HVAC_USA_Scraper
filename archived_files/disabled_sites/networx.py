"""Networx (networx.com) — rating only (no public review list)."""
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class NetworxRating(SiteScraper):
    SITE_ID = "networx"
    DOMAIN = "networx.com"
    HAS_REVIEWS = False

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()

        # Direct Networx search
        url = f"https://www.networx.com/search?q={quote_plus(f'{name} {city} {state}')}"
        if c.safe_get(sb, url, settle_seconds=(2.5, 4)):
            try:
                anchors = sb.execute_script(
                    "return Array.from(document.querySelectorAll("
                    "'a[href*=\"/contractor/\"], a[href*=\"/profile/\"], a[href*=\"/pro/\"]'"
                    ")).slice(0,5).map(a=>a.href);"
                ) or []
                for href in anchors:
                    if any(k in href for k in ("/contractor/","/profile/","/pro/")):
                        return href.split("?")[0]
            except Exception:
                pass

        # Google fallback
        cands = c.google_site_search(sb, "networx.com", name, city, state,
            website_url=row.get("website_url",""))
        for cand in cands:
            if "/contractor/" in cand or "/profile/" in cand:
                return cand.split("?")[0]
        return cands[0] if cands else None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in (".rating-stars", "[class*='rating']", "span.rating-value"):
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
