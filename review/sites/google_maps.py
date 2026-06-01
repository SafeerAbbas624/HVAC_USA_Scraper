"""Google Maps / Google Business Profile reviews scraper."""
import logging
import random
import re
import time
from urllib.parse import quote_plus

from ..base_scraper import SiteScraper
from . import _common as c

logger = logging.getLogger("review")


class GoogleMapsReviews(SiteScraper):
    SITE_ID = "google_maps"
    DOMAIN = "google.com"
    HAS_REVIEWS = True

    def search(self, sb, row):
        """Locate place URL, then transform to a deep-link that pre-selects
        the Reviews tab via `!9m1!1b1` appended to the data= param.

        Without this transform, Google Maps sometimes returns a "compact
        preview" panel without tabs (verified for small biz like Degree
        Heating, Waterbury CT). The deep-link forces the full place page
        with reviews tab already active and review cards rendered.

        Search-term priority:
          1. business_name + city + state            (when name is set)
          2. domain + city + state                   (fallback if (1) misses
                                                      OR if name is empty)
          3. None                                    (no name AND no domain)
        """
        name = (row.get("business_name") or "").strip()
        city = (row.get("business_city") or "").strip()
        state = (row.get("business_state") or "").strip()
        domain = c.domain_from_url(row.get("website_url", ""))

        cur = None
        # Try 1: name + city + state (only when name exists)
        if name:
            cur = self._search_query(
                sb, " ".join(p for p in [name, city, state] if p)
            )

        # Try 2: domain + city + state — fires when (a) name was missing,
        # or (b) name search above returned nothing.
        if not cur and domain:
            cur = self._search_query(
                sb, " ".join(p for p in [domain, city, state] if p)
            )

        if not cur:
            return None
        return self._to_reviews_url(cur)

    def _search_query(self, sb, q):
        """Run one /maps/search/ query and return the place URL if any."""
        if not q.strip():
            return None
        url = f"https://www.google.com/maps/search/{quote_plus(q)}?hl=en"
        if not c.safe_get(sb, url, settle_seconds=(4, 6)):
            return None
        cur = sb.get_current_url() or ""
        if "/maps/place/" in cur:
            return cur
        try:
            cards = sb.find_elements("css selector", "a.hfpxzc")
            if cards:
                cards[0].click()
                time.sleep(random.uniform(2.5, 4.0))
                cur = sb.get_current_url() or ""
                if "/maps/place/" in cur:
                    return cur
        except Exception:
            pass
        return None

    @staticmethod
    def _to_reviews_url(place_url: str) -> str:
        if "!9m1!1b1" in place_url:
            return place_url
        # Append after the data= param value (before any ? or # fragment)
        return re.sub(r"(data=[^?#]+)", r"\1!9m1!1b1", place_url)

    def scrape(self, sb, profile_url):
        # search() returns the deep-link URL with !9m1!1b1 already appended.
        # If we're not on that exact URL yet, navigate to it.
        target = self._to_reviews_url(profile_url) if profile_url else profile_url
        cur = sb.get_current_url() or ""
        if target and target != cur:
            if not c.safe_get(sb, target, settle_seconds=(4, 6)):
                return {"rating": None, "reviews": []}
        if c.is_no_reviews_page(sb):
            return {"rating": None, "reviews": []}

        # Discovered business name — used to backfill DB rows that were
        # scraped via the domain fallback (i.e. input had no business_name).
        discovered_name = None
        try:
            els = sb.find_elements("css selector", "h1.DUwDvf, div.lMbq3e h1, h1")
            for el in els:
                t = (el.text or "").strip()
                if t and len(t) < 200:
                    discovered_name = t
                    break
        except Exception:
            pass

        rating = None
        try:
            rating_el = sb.find_elements("css selector", "div.F7nice span[aria-hidden='true']")
            if rating_el:
                rating = c.extract_rating_from_element_text(rating_el[0].text)
        except Exception:
            pass
        if rating is None:
            try:
                rating = c.extract_first_rating(sb.get_text("div.F7nice"))
            except Exception:
                pass

        # === Reviews tab click — VERIFIED via harvest of Donnelly's profile ===
        # The place page has 3 tabs: Overview / Reviews / About.
        # Strategy A: aria-label='Reviews for <Business>' — verified to match
        # in harvest. Wait up to 20s for tabs to render (Google Maps is JS-heavy).
        clicked_reviews = False
        for attempt in range(3):
            try:
                sb.wait_for_element("button[role='tab']", timeout=20)
            except Exception:
                pass
            time.sleep(random.uniform(1.5, 2.5))

            try:
                # Verified primary selector
                els = sb.find_elements(
                    "css selector",
                    'button[role="tab"][aria-label*="Reviews"]'
                )
                if els:
                    sb.execute_script("arguments[0].click();", els[0])
                    clicked_reviews = True
                    break
            except Exception:
                pass

            # Fallback A: tab whose visible text equals "Reviews"
            if not clicked_reviews:
                try:
                    els = sb.find_elements(
                        "xpath",
                        "//button[@role='tab'][normalize-space(.)='Reviews']"
                    )
                    if els:
                        sb.execute_script("arguments[0].click();", els[0])
                        clicked_reviews = True
                        break
                except Exception:
                    pass

            # Fallback B: positional 2nd tab
            if not clicked_reviews:
                try:
                    tabs = sb.find_elements("css selector", "button[role='tab']")
                    if len(tabs) >= 2:
                        sb.execute_script("arguments[0].click();", tabs[1])
                        clicked_reviews = True
                        break
                except Exception:
                    pass
            time.sleep(1.5)

        if clicked_reviews:
            time.sleep(random.uniform(2.5, 4.0))
        else:
            logger.debug("[google_maps] failed to click Reviews tab after 3 attempts")

        # Find the scrollable reviews container — it's the m6QErb div whose
        # scrollHeight > clientHeight (only the right pane scrolls)
        scroll_js = """
            var ds = document.querySelectorAll('div[role="main"] div.m6QErb');
            var target = null;
            for (var i=0; i<ds.length; i++) {
                if (ds[i].scrollHeight > ds[i].clientHeight + 50) {
                    target = ds[i];
                }
            }
            if (target) target.scrollBy(0, 3000);
            return target ? target.scrollHeight : 0;
        """
        last_h = 0
        stagnant = 0
        for _ in range(60):  # up to ~60 scroll iterations to load 100 reviews
            try:
                h = sb.execute_script(scroll_js)
            except Exception:
                break
            if h == last_h:
                stagnant += 1
                if stagnant >= 3:  # 3 consecutive no-growth → list exhausted
                    break
            else:
                stagnant = 0
                last_h = h
            time.sleep(random.uniform(0.7, 1.1))

        # Expand any "More" buttons on individual reviews
        try:
            for b in sb.find_elements("css selector", "button.w8nwRe"):
                try:
                    b.click()
                    time.sleep(0.05)
                except Exception:
                    continue
        except Exception:
            pass

        reviews = []
        try:
            cards = sb.find_elements("css selector", "div.jftiEf")
            for card in cards[: self.MAX_REVIEWS]:
                author = c.attr_or_none(card.find_elements("css selector", "div.d4r55")[0]
                                        if card.find_elements("css selector", "div.d4r55") else card,
                                        "innerText") or c.text_or_none(card)
                rating_v = None
                try:
                    rs = card.find_elements("css selector", "span.kvMYJc")
                    if rs:
                        lab = rs[0].get_attribute("aria-label") or ""
                        m = re.search(r"(\d)", lab)
                        if m:
                            rating_v = int(m.group(1))
                except Exception:
                    pass
                date_v = None
                try:
                    de = card.find_elements("css selector", "span.rsqaWe")
                    if de:
                        date_v = de[0].text
                except Exception:
                    pass
                text_v = None
                try:
                    te = card.find_elements("css selector", "span.wiI7pd")
                    if te:
                        text_v = te[0].text
                except Exception:
                    pass
                reviews.append({
                    "author": author or "",
                    "rating": rating_v,
                    "date": date_v or "",
                    "text": text_v or "",
                })
        except Exception:
            pass

        return {
            "rating": rating,
            "reviews": reviews,
            "business_name": discovered_name,
        }
