"""Verify selectors against harvested profile.html files (offline, no browser).

For each site where we captured a real profile.html, runs candidate selectors
against the saved DOM and reports what they extract. This is how we verify
selectors BEFORE writing them into production code.

Usage: python -m review._verify_selectors
"""
import json
import os
import re
from bs4 import BeautifulSoup

H = os.path.join(os.path.dirname(__file__), "_harvest")


def _load(site_id, fname="profile.html"):
    path = os.path.join(H, site_id, fname)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _try(soup, selector):
    """CSS selector → list of (text, attrs)"""
    out = []
    for el in soup.select(selector):
        out.append({
            "text": el.get_text(" ", strip=True)[:200],
            "aria_label": el.get("aria-label", "")[:200],
            "class": " ".join(el.get("class") or [])[:100],
        })
    return out


def verify_trustpilot():
    print("\n" + "="*70 + "\nTRUSTPILOT — testing selectors against harvested profile\n" + "="*70)
    html = _load("trustpilot")
    if not html: return print("  no profile.html")
    s = BeautifulSoup(html, "html.parser")
    print(f"\n  Rating selectors:")
    for sel in [
        '[aria-label*="TrustScore"]',
        '[data-rating-typography]',
        'p[data-rating-typography]',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
        for x in r[:3]:
            print(f"          aria='{x['aria_label']}' text='{x['text'][:60]}'")
    print(f"\n  Review-card selectors:")
    for sel in [
        '[data-service-review-card-paper]',
        '[data-testid="service-review-card-v2"]',
        'article[data-service-review-card-paper]',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
    # Within first review card, find author/text/date/rating
    cards = s.select('[data-testid="service-review-card-v2"]')
    if cards:
        print(f"\n  First review card structure (data-testid='service-review-card-v2'):")
        c = cards[0]
        # Author
        for sel in ['[data-consumer-name-typography]', 'span[class*="consumer"]', 'a[href*="/users/"]']:
            els = c.select(sel)
            print(f"    author? '{sel}' → {len(els)} hits  text='{els[0].get_text(' ',strip=True)[:60] if els else ''}'")
        # Rating
        for sel in ['[data-service-review-rating]', 'div[class*="StarRating"] img', 'img[alt*="Rated"]']:
            els = c.select(sel)
            if els:
                print(f"    rating? '{sel}' → {len(els)} hits  alt='{els[0].get('alt','')[:80]}' attr='{els[0].get('data-service-review-rating','')}'")
        # Date
        for sel in ['time', '[data-review-date-typography]']:
            els = c.select(sel)
            print(f"    date?  '{sel}' → {len(els)} hits  datetime='{els[0].get('datetime','') if els else ''}' text='{els[0].get_text(' ',strip=True)[:40] if els else ''}'")
        # Text body
        for sel in ['[data-service-review-text-typography]', 'p[data-service-review-text-typography]', 'p']:
            els = c.select(sel)
            print(f"    text?  '{sel}' → {len(els)} hits  first='{els[0].get_text(' ',strip=True)[:80] if els else ''}'")


def verify_yelp():
    print("\n" + "="*70 + "\nYELP — Dunn's HVAC profile\n" + "="*70)
    html = _load("yelp", "profile_fallback.html")
    if not html: return print("  no profile_fallback.html")
    s = BeautifulSoup(html, "html.parser")
    print(f"\n  Rating selectors:")
    for sel in [
        '[aria-label*="star rating"]',
        'div[role="img"][aria-label*="star"]',
        '[data-testid="BizHeaderReviewCount"]',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
        for x in r[:2]:
            print(f"          aria='{x['aria_label']}' text='{x['text'][:60]}'")
    print(f"\n  Review-card selectors:")
    for sel in [
        '[id^="review_"]',
        'li div.review',
        'section[aria-label*="Reviews"] li',
        'main section ul li',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
    # Look for any element with "Stars" in the html (review ratings on Yelp)
    rating_imgs = s.find_all('img', alt=re.compile(r'star', re.I))
    print(f"\n  <img alt='X star rating'>: {len(rating_imgs)}")
    for img in rating_imgs[:3]:
        print(f"    alt='{img.get('alt','')[:60]}'")


def verify_yellowpages():
    print("\n" + "="*70 + "\nYELLOWPAGES — ARS profile\n" + "="*70)
    html = _load("yellowpages")
    if not html: return print("  no profile.html")
    s = BeautifulSoup(html, "html.parser")
    print(f"\n  Rating selectors:")
    for sel in [
        '.yp-ratings span',
        '.rating-stars',
        '[itemprop="ratingValue"]',
        'p.average-rating',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
        for x in r[:2]:
            print(f"          text='{x['text'][:80]}'")
    print(f"\n  Review-card selectors:")
    for sel in [
        '.reviews-container article',
        '.reviews-container .review-response',
        'article.review-response',
        '.review-content',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")


def verify_google_maps():
    print("\n" + "="*70 + "\nGOOGLE MAPS — 205 Heating profile (fallback)\n" + "="*70)
    html = _load("google_maps", "profile_fallback.html")
    if not html: return print("  no profile_fallback.html")
    s = BeautifulSoup(html, "html.parser")
    print(f"\n  Rating selectors:")
    for sel in [
        'div.F7nice span[aria-hidden="true"]',
        'div.F7nice',
        'div[class*="F7nice"] span',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
        for x in r[:2]:
            print(f"          text='{x['text'][:60]}'")
    print(f"\n  Review-card selectors (after Reviews tab):")
    for sel in [
        'div.jftiEf',
        'div[data-review-id]',
    ]:
        r = _try(s, sel)
        print(f"    [{len(r):>3d}] {sel}")
    # Note: Google Maps reviews are JS-rendered, often empty in static HTML
    # snapshot. The lack of jftiEf in the harvested page is expected if we
    # didn't click the Reviews tab during harvest.


def verify_bbb_search():
    print("\n" + "="*70 + "\nBBB — search results profile-link selector\n" + "="*70)
    html = _load("bbb", "search.html")
    if not html: return print("  no search.html")
    s = BeautifulSoup(html, "html.parser")
    # Profile URL pattern: /us/<state>/<city>/profile/<category>/<slug>-<id>
    # But NEVER doubleclick / google ad redirects
    pattern = re.compile(r'^/(?:us|ca)/[a-z]{2,3}/[\w-]+/profile/[\w-]+/[\w-]+', re.I)
    candidates = []
    for a in s.find_all("a", href=True):
        href = a.get("href","")
        if pattern.match(href):
            candidates.append(href)
    candidates = list(dict.fromkeys(candidates))  # dedup, preserve order
    print(f"  /us/<state>/.../profile/<cat>/<slug> hits: {len(candidates)}")
    for c in candidates[:5]:
        print(f"    {c}")


if __name__ == "__main__":
    verify_trustpilot()
    verify_yelp()
    verify_yellowpages()
    verify_google_maps()
    verify_bbb_search()
