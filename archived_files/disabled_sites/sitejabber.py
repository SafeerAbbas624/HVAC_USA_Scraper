"""SiteJabber (sitejabber.com) — domain-based search works best."""
import re
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


class SiteJabberReviews(SiteScraper):
    SITE_ID = "sitejabber"
    DOMAIN = "sitejabber.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        web = (row.get("website_url") or "").strip()
        dom = c.domain_from_url(web)
        if dom:
            direct = f"https://www.sitejabber.com/reviews/{dom}"
            if c.safe_get(sb, direct, settle_seconds=(2, 3)):
                try:
                    title = (sb.get_title() or "").lower()
                    if "not found" not in title and "404" not in title:
                        return direct
                except Exception:
                    return direct
        name = (row.get("business_name") or "").strip()
        if not name: return None
        url = f"https://www.sitejabber.com/search?q={quote_plus(name)}"
        if not c.safe_get(sb, url, settle_seconds=(2, 3)):
            return None
        try:
            anchors = sb.find_elements("css selector",
                "a[href*='/reviews/'], a.business-link")
            if anchors:
                href = anchors[0].get_attribute("href")
                if href and "/reviews/" in href:
                    return href.split("?")[0]
        except Exception:
            pass
        return None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}
        rating = None
        for sel in ("div.rating-stars",
                    "[itemprop='ratingValue']",
                    "span.rating"):
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
        c.click_load_more(sb, ["a.load-more", "button.load-more", "a.more-reviews"])

        reviews = []
        try:
            cards = sb.find_elements("css selector",
                "div.review, article.review, [itemprop='review']")
            for card in cards[: self.MAX_REVIEWS]:
                author, date, text, rv = "", "", "", None
                try:
                    a = card.find_elements("css selector",
                        ".author, [itemprop='author'], .reviewer-name")
                    if a: author = (a[0].text or "").strip()
                except Exception: pass
                try:
                    d = card.find_elements("css selector", "time, .date")
                    if d: date = (d[0].text or "").strip()
                except Exception: pass
                try:
                    t = card.find_elements("css selector",
                        "[itemprop='reviewBody'], .review-text, p")
                    if t: text = (t[0].text or "").strip()
                except Exception: pass
                try:
                    r = card.find_elements("css selector",
                        "[itemprop='ratingValue'], [class*='rating']")
                    if r:
                        m = re.search(r"(\d(?:\.\d)?)", r[0].text or "")
                        if m: rv = float(m.group(1))
                except Exception: pass
                if author or text:
                    reviews.append({"author": author, "rating": rv, "date": date, "text": text})
        except Exception: pass
        return {"rating": rating, "reviews": reviews}
