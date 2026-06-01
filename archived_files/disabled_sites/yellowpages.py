"""Yellow Pages (yellowpages.com) — direct search + listing."""
import random
import re
import time
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class YellowPagesReviews(SiteScraper):
    SITE_ID = "yellowpages"
    DOMAIN = "yellowpages.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        """YP search returns multiple .result h2 a anchors; pick the one whose
        link text best matches the business name (verified from harvest).
        URL pattern: /<city>-<state>/mip/<slug>-<id>"""
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()
        url = (f"https://www.yellowpages.com/search?"
               f"search_terms={quote_plus(name)}"
               f"&geo_location_terms={quote_plus(f'{city}, {state}')}")
        if not c.safe_get(sb, url, settle_seconds=(2.5, 4)):
            return None
        try:
            # JS-side: collect all .result h2 a with text + href, pick best match
            results = sb.execute_script(
                "return Array.from(document.querySelectorAll('.result h2 a, a.business-name'))"
                ".map(a => ({href: a.getAttribute('href'), text: (a.textContent||'').trim()}))"
                ".filter(o => o.href && o.href.includes('/mip/'))"
                ".slice(0, 20);"
            ) or []
        except Exception:
            results = []
        if results:
            best = None
            best_score = 0.0
            for r in results:
                s = c.name_match(r.get("text", ""), name)
                if s > best_score:
                    best_score = s
                    best = r
            chosen = best or results[0]
            href = chosen["href"]
            if not href.startswith("http"):
                href = "https://www.yellowpages.com" + href
            return href.split("?")[0]
        # Fallback to Google
        cands = c.google_site_search(sb, "yellowpages.com", name, city, state,
                                     website_url=row.get("website_url",""))
        for cand in cands:
            if "/mip/" in cand:
                return cand.split("?")[0]
        return cands[0] if cands else None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in ("div.ratings-and-reviews div.average-rating",
                    "p.average-rating", "[class*='rating']"):
            try:
                els = sb.find_elements("css selector", sel)
                if els:
                    rating = c.extract_rating_from_element_text(els[0].text)
                    if rating: break
            except Exception: continue

        c.scroll_down(sb, n_times=8)
        c.click_load_more(sb, ["a.load-more", "button.load-more", "a.pagination-next"])

        reviews = []
        try:
            cards = sb.find_elements("css selector", "article.review-response, div.review")
            for card in cards[: self.MAX_REVIEWS]:
                author, date, text, rv = "", "", "", None
                try:
                    a = card.find_elements("css selector", ".author-name, .reviewer-name")
                    if a: author = (a[0].text or "").strip()
                except Exception: pass
                try:
                    d = card.find_elements("css selector", ".date, time")
                    if d: date = (d[0].text or "").strip()
                except Exception: pass
                try:
                    t = card.find_elements("css selector", ".review-text, p")
                    if t: text = (t[0].text or "").strip()
                except Exception: pass
                try:
                    r = card.find_elements("css selector", "[class*='rating']")
                    if r:
                        m = re.search(r"(\d(?:\.\d)?)", r[0].text or "")
                        if m: rv = float(m.group(1))
                except Exception: pass
                if author or text:
                    reviews.append({"author": author, "rating": rv, "date": date, "text": text})
        except Exception: pass
        return {"rating": rating, "reviews": reviews}
