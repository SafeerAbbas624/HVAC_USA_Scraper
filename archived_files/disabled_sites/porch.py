"""Porch (porch.com) — pro profiles + reviews."""
import re
from ..base_scraper import SiteScraper
from . import _common as c


class PorchReviews(SiteScraper):
    SITE_ID = "porch"
    DOMAIN = "porch.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()

        # Direct Porch search
        from urllib.parse import quote_plus
        url = (f"https://porch.com/search?"
               f"keyword={quote_plus(name)}&location={quote_plus(f'{city}, {state}')}")
        if c.safe_get(sb, url, settle_seconds=(2.5, 4)):
            try:
                anchors = sb.execute_script(
                    "return Array.from(document.querySelectorAll("
                    "'a[href*=\"/pp/\"], a[href$=\"/pp\"], a[href*=\"/professionals/\"]'"
                    ")).slice(0,5).map(a=>a.href);"
                ) or []
                for href in anchors:
                    if "/pp" in href or "/professionals/" in href:
                        return href.split("?")[0]
            except Exception:
                pass

        # Google fallback
        cands = c.google_site_search(sb, "porch.com", name, city, state,
            website_url=row.get("website_url",""))
        for cand in cands:
            if "/pp" in cand or "/professionals/" in cand:
                return cand.split("?")[0]
        return cands[0] if cands else None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 5)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in ("span.rating-value", "[aria-label*='star']",
                    "div[class*='Rating'] span"):
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

        c.scroll_down(sb, n_times=10)
        c.click_load_more(sb, ["button.load-more", "a.load-more"])

        reviews = []
        try:
            cards = sb.find_elements("css selector", "div.review, article.review")
            for card in cards[: self.MAX_REVIEWS]:
                author, date, text, rv = "", "", "", None
                try:
                    a = card.find_elements("css selector", ".reviewer-name, .author")
                    if a: author = (a[0].text or "").strip()
                except Exception: pass
                try:
                    d = card.find_elements("css selector", ".review-date, time")
                    if d: date = (d[0].text or "").strip()
                except Exception: pass
                try:
                    t = card.find_elements("css selector", ".review-text, p")
                    if t: text = (t[0].text or "").strip()
                except Exception: pass
                try:
                    r = card.find_elements("css selector", "[aria-label*='star']")
                    if r:
                        m = re.search(r"(\d(?:\.\d)?)", r[0].get_attribute("aria-label") or "")
                        if m: rv = float(m.group(1))
                except Exception: pass
                if author or text:
                    reviews.append({"author": author, "rating": rv, "date": date, "text": text})
        except Exception: pass
        return {"rating": rating, "reviews": reviews}
