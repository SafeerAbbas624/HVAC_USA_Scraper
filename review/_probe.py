"""One-off probe to diagnose discovery failures.

Tests direct site search vs Google site-search for one real contractor row,
showing exactly what HTML/URLs each path produces.

Usage:
  python -m review._probe
"""
import logging
import sys
from urllib.parse import quote_plus, urlparse

from . import config
from . import browser
from . import proxies as proxies_mod
from .sites._common import google_site_search

config.setup_logging()
logger = logging.getLogger("review")

ROW = {
    "business_name": "205 Heating & Cooling LLC",
    "business_city": "Birmingham",
    "business_state": "AL",
    "website_url": "",
}


def probe_yelp_direct(sb, row):
    name = row["business_name"]
    city = row["business_city"]
    state = row["business_state"]
    url = (f"https://www.yelp.com/search?"
           f"find_desc={quote_plus(name)}"
           f"&find_loc={quote_plus(f'{city}, {state}')}")
    print(f"\n=== YELP DIRECT SEARCH ===\nURL: {url}")
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=6)
    except Exception as e:
        print(f"  open failed: {e}")
        return
    import time, random
    time.sleep(random.uniform(3, 5))
    print(f"  current_url: {sb.get_current_url()[:120]}")
    print(f"  title: {(sb.get_title() or '')[:120]}")
    body = sb.get_page_source() or ""
    print(f"  body_size: {len(body)}")
    # Did we get blocked?
    block_signals = ["captcha", "perimeterx", "verify you are human", "px-captcha", "are you a human"]
    found_blocks = [b for b in block_signals if b in body.lower()[:20000]]
    if found_blocks:
        print(f"  BLOCKED — signals: {found_blocks}")
    # Find biz links
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/biz/\"]')).slice(0,5).map(a=>a.href);"
        )
        print(f"  /biz/ links: {len(anchors or [])}")
        for h in (anchors or [])[:3]:
            print(f"    {h[:120]}")
    except Exception as e:
        print(f"  JS error: {e}")


def probe_bbb_direct(sb, row):
    name = row["business_name"]
    city = row["business_city"]
    state = row["business_state"]
    url = (f"https://www.bbb.org/search?"
           f"find_text={quote_plus(name)}"
           f"&find_loc={quote_plus(f'{city}, {state}')}")
    print(f"\n=== BBB DIRECT SEARCH ===\nURL: {url}")
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=6)
    except Exception as e:
        print(f"  open failed: {e}")
        return
    import time, random
    time.sleep(random.uniform(3, 5))
    print(f"  current_url: {sb.get_current_url()[:120]}")
    print(f"  title: {(sb.get_title() or '')[:120]}")
    body = sb.get_page_source() or ""
    print(f"  body_size: {len(body)}")
    block_signals = ["just a moment", "cf-browser-verification", "challenge-platform"]
    found_blocks = [b for b in block_signals if b in body.lower()[:20000]]
    if found_blocks:
        print(f"  BLOCKED (Cloudflare) — signals: {found_blocks}")
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/profile/\"]')).slice(0,5).map(a=>a.href);"
        )
        print(f"  /profile/ links: {len(anchors or [])}")
        for h in (anchors or [])[:3]:
            print(f"    {h[:120]}")
    except Exception as e:
        print(f"  JS error: {e}")


def probe_yp_direct(sb, row):
    name = row["business_name"]
    city = row["business_city"]
    state = row["business_state"]
    url = (f"https://www.yellowpages.com/search?"
           f"search_terms={quote_plus(name)}"
           f"&geo_location_terms={quote_plus(f'{city}, {state}')}")
    print(f"\n=== YELLOWPAGES DIRECT SEARCH ===\nURL: {url}")
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=6)
    except Exception as e:
        print(f"  open failed: {e}")
        return
    import time, random
    time.sleep(random.uniform(3, 5))
    print(f"  current_url: {sb.get_current_url()[:120]}")
    print(f"  title: {(sb.get_title() or '')[:120]}")
    body = sb.get_page_source() or ""
    print(f"  body_size: {len(body)}")
    try:
        # Multiple selector attempts
        for sel in ("a.business-name", "a[class*='business-name']",
                    "a.track-visit-website", "h3.n a"):
            anchors = sb.execute_script(
                f"return Array.from(document.querySelectorAll('{sel}')).slice(0,5).map(a=>a.href);"
            )
            if anchors:
                print(f"  selector '{sel}': {len(anchors)} matches")
                for h in anchors[:3]:
                    print(f"    {h[:120]}")
                break
        else:
            print(f"  no business-link selectors matched")
    except Exception as e:
        print(f"  JS error: {e}")


def probe_google_site_search(sb, row, target):
    print(f"\n=== GOOGLE site:{target} ===")
    candidates = google_site_search(
        sb, target, row["business_name"], row["business_city"], row["business_state"],
    )
    print(f"  candidates: {len(candidates)}")
    for c in candidates[:5]:
        print(f"    {c[:120]}")


def main():
    proxies = proxies_mod.load_proxies()
    proxy = proxies_mod.pick_proxy(proxies) if proxies else None
    print(f"Using proxy: {proxy}\n")

    proxy_str, bridge = browser._build_proxy_string(proxy)

    from seleniumbase import SB
    chromium_args = [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-blink-features=AutomationControlled", "--mute-audio",
    ]
    sb_kwargs = {
        "uc": True, "headless": False, "incognito": True,
        "chromium_arg": ",".join(chromium_args),
    }
    if browser._CHROME_BINARY:
        sb_kwargs["binary_location"] = browser._CHROME_BINARY
    if proxy_str:
        sb_kwargs["proxy"] = proxy_str

    try:
        from sbvirtualdisplay import Display
        disp = Display(visible=0, size=(1920, 1080))
        disp.start()
    except Exception:
        disp = None

    try:
        with SB(**sb_kwargs) as sb:
            sb.driver.set_page_load_timeout(45)
            print(f"=== Probing for: {ROW['business_name']} ({ROW['business_city']}, {ROW['business_state']}) ===")
            probe_yelp_direct(sb, ROW)
            probe_bbb_direct(sb, ROW)
            probe_yp_direct(sb, ROW)
            probe_google_site_search(sb, ROW, "yelp.com/biz")
            probe_google_site_search(sb, ROW, "bbb.org")
    finally:
        if bridge:
            try: bridge.stop()
            except: pass
        if disp:
            try: disp.stop()
            except: pass


if __name__ == "__main__":
    main()
