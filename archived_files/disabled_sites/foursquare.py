"""Foursquare (foursquare.com) — also covers CitySearch (citysearch.com) as fallback."""
import re
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class FoursquareReviews(SiteScraper):
    SITE_ID = "foursquare"
    DOMAIN = "foursquare.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()
        url = (f"https://foursquare.com/explore?"
               f"q={quote_plus(name)}&near={quote_plus(f'{city}, {state}')}")
        if c.safe_get(sb, url, settle_seconds=(3, 4)):
            try:
                anchors = sb.find_elements("css selector",
                    "a[href*='/v/'], a.venueLink")
                if anchors:
                    href = anchors[0].get_attribute("href")
                    if href and "/v/" in href:
                        return href.split("?")[0]
            except Exception:
                pass
        # Fallback: Google
        cands = c.google_site_search(sb, "foursquare.com", name, city, state,
                                     website_url=row.get("website_url",""))
        for cand in cands:
            if "/v/" in cand:
                return cand.split("?")[0]
        # Final fallback: CitySearch via Google
        cs = c.google_site_search(sb, "citysearch.com", name, city, state)
        return cs[0] if cs else None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in ("span.venueRatingValue", "div[class*='Rating']", "[class*='rating']"):
            try:
                els = sb.find_elements("css selector", sel)
                if els:
                    rating = c.extract_rating_from_element_text(els[0].text)
                    if rating: break
            except Exception: continue
        if rating is None:
            try: rating = c.extract_first_rating(sb.get_text("body")[:5000])
            except Exception: pass

        c.scroll_down(sb, n_times=8)
        reviews = []
        try:
            cards = sb.find_elements("css selector",
                "div.tipContents, li.tip, div.review")
            for card in cards[: self.MAX_REVIEWS]:
                author, date, text, rv = "", "", "", None
                try:
                    a = card.find_elements("css selector", ".userName, .author")
                    if a: author = (a[0].text or "").strip()
                except Exception: pass
                try:
                    d = card.find_elements("css selector", "time, .tipDate")
                    if d: date = (d[0].text or "").strip()
                except Exception: pass
                try:
                    t = card.find_elements("css selector", ".tipText, p, .review-text")
                    if t: text = (t[0].text or "").strip()
                except Exception: pass
                if author or text:
                    reviews.append({"author": author, "rating": rv, "date": date, "text": text})
        except Exception: pass
        return {"rating": rating, "reviews": reviews}
