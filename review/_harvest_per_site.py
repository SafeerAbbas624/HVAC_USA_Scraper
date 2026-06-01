"""Per-site deep harvest: navigate to a known REAL profile URL with reviews,
let it fully render (and scroll/click as needed), then save the post-render DOM.

This applies the same troubleshooting principle as Google Maps:
1. Use a known good direct-profile URL (avoiding broken discovery)
2. Capture post-interaction DOM (scrolling, expanding "more" buttons, etc.)
3. Save HTML for inspection → write VERIFIED selectors

Usage: python -m review._harvest_per_site bbb yelp yp trustpilot angi
"""
import os
import random
import sys
import time

from . import config
from . import browser
from . import proxies as proxies_mod

config.setup_logging()

OUT = os.path.join(os.path.dirname(__file__), "_harvest_v2")
os.makedirs(OUT, exist_ok=True)


# Known REAL profile URLs (with actual reviews) for each site.
# These bypass discovery so we can verify scrape() selectors against
# real post-render DOMs.
PROFILE_URLS = {
    # BBB Allgood Home Services (returned by earlier harvest's BBB search)
    "bbb": "https://www.bbb.org/us/ga/lawrenceville/profile/heating-and-air-conditioning/allgood-home-services-0443-91847917",
    # YellowPages ARS Rescue Rooter Atlanta (found in search.html earlier)
    "yp":  "https://www.yellowpages.com/atlanta-ga/mip/ars-rescue-rooter-562952399",
    # Trustpilot ARS — already worked; pick a small sample we want to verify
    "trustpilot": "https://www.trustpilot.com/review/ars.com",
    # Yelp Dunn's HVAC (returned by earlier harvest's Yelp fallback)
    "yelp": "https://www.yelp.com/biz/dunns-hvac-plumbing-and-electrical-pelham-6",
    # Angi: try a real specific service-pro URL
    "angi": "https://www.angi.com/companylist/us/ga/atlanta/ars-rescue-rooter-reviews-7066022.htm",
}


def _save(site, fn, content):
    d = os.path.join(OUT, site)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
        f.write(content or "")


def _scroll_and_expand(sb, n=15):
    """Generic scroll + expand 'More' buttons."""
    for _ in range(n):
        try:
            sb.execute_script("window.scrollBy(0, 1500);")
            # Click any 'More' / 'Read more' / 'Show more' buttons
            sb.execute_script("""
                document.querySelectorAll('button, a').forEach(b => {
                    var t = (b.textContent || '').toLowerCase().trim();
                    if (t === 'more' || t === 'read more' || t === 'show more' ||
                        t === 'see more reviews' || t.startsWith('load more')) {
                        try { b.click(); } catch (e) {}
                    }
                });
            """)
        except Exception:
            break
        time.sleep(random.uniform(0.6, 1.2))


def harvest(sb, site, url):
    print(f"\n=== {site} ===\nGET {url}")
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=8)
    except Exception as e:
        print(f"  open failed: {e}")
        return
    time.sleep(random.uniform(4, 6))

    # Try captcha solver (no-op if no captcha)
    try:
        sb.uc_gui_click_captcha()
    except Exception:
        pass

    cur = sb.get_current_url() or ""
    print(f"  landed: {cur[:130]}")

    _save(site, "0_initial.html", sb.get_page_source())

    # Generic scroll + expand
    _scroll_and_expand(sb, n=20)
    _save(site, "1_after_scroll.html", sb.get_page_source())

    # Site-specific deeper interactions
    try:
        if site == "yelp":
            # Scroll into Recommended Reviews
            sb.execute_script(
                "var s = document.querySelector("
                "'section[aria-label=\"Recommended Reviews\"], "
                "[aria-label=\"Recommended Reviews\"]'"
                "); if (s) s.scrollIntoView({block:'start'});"
            )
            time.sleep(3)
            _scroll_and_expand(sb, n=10)
            _save(site, "2_yelp_reviews_section.html", sb.get_page_source())
        elif site == "bbb":
            # BBB: click "Customer Reviews" tab if present
            sb.execute_script(
                "document.querySelectorAll('a, button').forEach(b => {"
                "  var t=(b.textContent||'').toLowerCase().trim();"
                "  if (t==='customer reviews' || t.includes('customer reviews')) {"
                "    try { b.click(); } catch(e) {}"
                "  }"
                "});"
            )
            time.sleep(3)
            _scroll_and_expand(sb, n=15)
            _save(site, "2_bbb_reviews_tab.html", sb.get_page_source())
        elif site == "yp":
            # YP: scroll to reviews container
            sb.execute_script(
                "var c = document.querySelector('.reviews-container, [class*=\"reviews\"]');"
                "if (c) c.scrollIntoView({block:'start'});"
            )
            time.sleep(2)
            _scroll_and_expand(sb, n=10)
            _save(site, "2_yp_reviews_section.html", sb.get_page_source())
    except Exception as e:
        print(f"  interact error: {e}")

    final = sb.get_page_source() or ""
    print(f"  final size: {len(final)}")
    _save(site, "3_final.html", final)


def main(sites):
    proxy = proxies_mod.pick_proxy(proxies_mod.load_proxies())
    proxy_str, bridge = browser._build_proxy_string(proxy)

    from seleniumbase import SB
    sb_kwargs = {
        "uc": True, "undetectable": True, "test": True, "locale": "en",
        "ad_block": True, "headless": False, "incognito": True,
        "chromium_arg": "--no-sandbox,--disable-gpu,--disable-blink-features=AutomationControlled",
    }
    if browser._CHROME_BINARY:
        sb_kwargs["binary_location"] = browser._CHROME_BINARY
    if proxy_str:
        sb_kwargs["proxy"] = proxy_str

    try:
        from sbvirtualdisplay import Display
        disp = Display(visible=0, size=(1920, 1080)); disp.start()
    except Exception:
        disp = None

    # Open a FRESH SB session per site to avoid cross-site contamination
    # (a misclick during scroll on one site can hijack subsequent navigations).
    try:
        for site in sites:
            if site not in PROFILE_URLS:
                print(f"  unknown site: {site}"); continue
            with SB(**sb_kwargs) as sb:
                sb.driver.set_page_load_timeout(45)
                harvest(sb, site, PROFILE_URLS[site])
    finally:
        if bridge:
            try: bridge.stop()
            except: pass
        if disp:
            try: disp.stop()
            except: pass


if __name__ == "__main__":
    sites = sys.argv[1:] if len(sys.argv) > 1 else list(PROFILE_URLS.keys())
    main(sites)
