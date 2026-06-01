"""Trustpilot (trustpilot.com) — domain-based search; reviews paginated.

Selectors verified from harvested profile.html (ars.com on 2026-04-28):
  - Rating:          aria-label="TrustScore X.X out of 5"
                     OR p[data-rating-typography] (numeric value as text)
  - Review cards:    [data-testid="service-review-card-v2"]
  - Per card:
      author:        [data-consumer-name-typography]
      rating value:  img[alt^="Rated"] (alt="Rated 4 out of 5 stars")
                     OR [data-service-review-rating] (attr value 1-5)
      date:          time[datetime]
      text body:     [data-service-review-text-typography]
"""
import re
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c


_RATED_X_RX = re.compile(r"Rated\s+(\d(?:\.\d)?)\s+out of 5", re.I)
_TRUSTSCORE_RX = re.compile(r"TrustScore\s+(\d(?:\.\d)?)", re.I)


class TrustpilotReviews(SiteScraper):
    SITE_ID = "trustpilot"
    DOMAIN = "trustpilot.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        # Trustpilot stores reviews per domain — try direct domain URL first
        web = (row.get("website_url") or "").strip()
        dom = c.domain_from_url(web)
        if dom:
            direct = f"https://www.trustpilot.com/review/{dom}"
            if c.safe_get(sb, direct, settle_seconds=(2, 3)):
                try:
                    title = (sb.get_title() or "").lower()
                    if "not found" not in title and "404" not in title:
                        return direct
                except Exception:
                    return direct
        # Fallback: Trustpilot search
        name = (row.get("business_name") or "").strip()
        if not name: return None
        url = f"https://www.trustpilot.com/search?query={quote_plus(name)}"
        if not c.safe_get(sb, url, settle_seconds=(2, 3)):
            return None
        try:
            anchors = sb.execute_script(
                "return Array.from(document.querySelectorAll("
                "'a[href*=\"/review/\"], a[name=\"business-unit-card\"]'"
                ")).slice(0,5).map(a=>a.href);"
            ) or []
            for href in anchors:
                if "/review/" in href:
                    return href.split("?")[0]
        except Exception:
            pass
        return None

    def scrape(self, sb, profile_url):
        if not c.safe_get(sb, profile_url, settle_seconds=(3, 4)):
            return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}

        # --- Rating ---
        rating = None
        try:
            els = sb.find_elements("css selector", '[aria-label*="TrustScore"]')
            for el in els:
                lbl = el.get_attribute("aria-label") or ""
                m = _TRUSTSCORE_RX.search(lbl)
                if m:
                    v = float(m.group(1))
                    if 0.5 <= v <= 5.0:    # reject 0.0 = "no rating yet"
                        rating = v
                        break
        except Exception:
            pass
        if rating is None:
            try:
                els = sb.find_elements("css selector", "p[data-rating-typography]")
                if els:
                    rating = c.extract_rating_from_element_text(els[0].text)
            except Exception:
                pass

        # --- Reviews (paginated; up to 10 pages) ---
        reviews = []
        for page in range(1, 11):
            if page > 1:
                paged = profile_url + ("&" if "?" in profile_url else "?") + f"page={page}"
                if not c.safe_get(sb, paged, settle_seconds=(2, 3)):
                    break
            c.scroll_down(sb, n_times=4, pause=(0.4, 0.8))
            try:
                cards = sb.find_elements("css selector",
                    '[data-testid="service-review-card-v2"]')
            except Exception:
                cards = []
            if not cards:
                break
            page_reviews = self._extract_cards(cards)
            if not page_reviews:
                break
            reviews.extend(page_reviews)
            if len(reviews) >= self.MAX_REVIEWS:
                break

        return {"rating": rating, "reviews": reviews[: self.MAX_REVIEWS]}

    @staticmethod
    def _extract_cards(cards):
        out = []
        for card in cards:
            author, date, text, rv = "", "", "", None
            # Author
            try:
                a = card.find_elements("css selector",
                    '[data-consumer-name-typography]')
                if a:
                    author = (a[0].text or "").strip()
            except Exception:
                pass
            # Rating: prefer img alt "Rated X out of 5"
            try:
                imgs = card.find_elements("css selector", 'img[alt*="Rated"]')
                if imgs:
                    m = _RATED_X_RX.search(imgs[0].get_attribute("alt") or "")
                    if m:
                        rv = float(m.group(1))
            except Exception:
                pass
            if rv is None:
                try:
                    rs = card.find_elements("css selector",
                        '[data-service-review-rating]')
                    if rs:
                        v = rs[0].get_attribute("data-service-review-rating")
                        if v:
                            rv = float(v)
                except Exception:
                    pass
            # Date
            try:
                t = card.find_elements("css selector", "time")
                if t:
                    date = (t[0].get_attribute("datetime")
                            or t[0].text or "").strip()
            except Exception:
                pass
            # Text body
            try:
                tx = card.find_elements("css selector",
                    '[data-service-review-text-typography]')
                if tx:
                    text = (tx[0].text or "").strip()
            except Exception:
                pass
            # Skip placeholder / empty cards
            if not text and not author:
                continue
            out.append({"author": author, "rating": rv,
                        "date": date, "text": text})
        return out
