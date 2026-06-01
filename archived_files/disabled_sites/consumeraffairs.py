"""ConsumerAffairs (consumeraffairs.com) — search + reviews."""
import re
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class ConsumerAffairsReviews(SiteScraper):
    SITE_ID = "consumeraffairs"
    DOMAIN = "consumeraffairs.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        name = (row.get("business_name") or "").strip()
        if not name: return None
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()

        # Direct ConsumerAffairs search
        url = f"https://www.consumeraffairs.com/search?query={quote_plus(name)}"
        if c.safe_get(sb, url, settle_seconds=(2.5, 4)):
            try:
                anchors = sb.execute_script(
                    "return Array.from(document.querySelectorAll("
                    "'a[href*=\"/business/\"], a[href*=\"/online/\"]'"
                    ")).slice(0,5).map(a=>a.href);"
                ) or []
                for href in anchors:
                    if "/business/" in href or "/online/" in href:
                        return href.split("?")[0]
            except Exception:
                pass

        # Google fallback
        cands = c.google_site_search(sb, "consumeraffairs.com", name, city, state,
            website_url=row.get("website_url",""))
        for cand in cands:
            if "/business/" in cand or "/online/" in cand:
                return cand.split("?")[0]
        return cands[0] if cands else None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in ("div.rating-summary span.rating",
                    "[itemprop='ratingValue']", "span.rating-value"):
            try:
                els = sb.find_elements("css selector", sel)
                if els:
                    rating = c.extract_rating_from_element_text(els[0].text)
                    if rating: break
            except Exception: continue
        if rating is None:
            try: rating = c.extract_first_rating(sb.get_text("body")[:5000])
            except Exception: pass

        c.scroll_down(sb, n_times=10)
        c.click_load_more(sb, ["a.load-more", "button.load-more"])

        reviews = []
        try:
            cards = sb.find_elements("css selector",
                "div.rvw, div.review, article[class*='review']")
            for card in cards[: self.MAX_REVIEWS]:
                author, date, text, rv = "", "", "", None
                try:
                    a = card.find_elements("css selector",
                        ".rvw-aut__inf-nm, .reviewer-name, [itemprop='author']")
                    if a: author = (a[0].text or "").strip()
                except Exception: pass
                try:
                    d = card.find_elements("css selector", "time, .rvw-aut__inf-dt")
                    if d: date = (d[0].text or "").strip()
                except Exception: pass
                try:
                    t = card.find_elements("css selector",
                        ".rvw-bd p, p[itemprop='reviewBody'], .review-body")
                    if t: text = (t[0].text or "").strip()
                except Exception: pass
                try:
                    r = card.find_elements("css selector", "[itemprop='ratingValue'], [class*='rating']")
                    if r:
                        m = re.search(r"(\d(?:\.\d)?)", r[0].text or r[0].get_attribute("content") or "")
                        if m: rv = float(m.group(1))
                except Exception: pass
                if author or text:
                    reviews.append({"author": author, "rating": rv, "date": date, "text": text})
        except Exception: pass
        return {"rating": rating, "reviews": reviews}
