"""Focused Google Maps harvest with explicit Reviews tab click.

Goal: capture the DOM AFTER clicking the Reviews tab + scrolling, so we can
verify the review-card selectors against actual post-interaction state.

Saves to review/_harvest/google_maps/profile_with_reviews.html
"""
import os
import random
import time
from urllib.parse import quote_plus

from . import config
from . import browser
from . import proxies as proxies_mod

config.setup_logging()

ROW = {
    "business_name": "Degree Heating & Cooling",
    "business_city": "Waterbury",
    "business_state": "CT",
}
OUT_DIR = os.path.join(os.path.dirname(__file__), "_harvest", "google_maps_r20")
os.makedirs(OUT_DIR, exist_ok=True)


def main():
    proxy = proxies_mod.pick_proxy(proxies_mod.load_proxies())
    proxy_str, bridge = browser._build_proxy_string(proxy)

    from seleniumbase import SB
    chromium_args = [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-blink-features=AutomationControlled", "--mute-audio",
    ]
    sb_kwargs = {
        "uc": True, "headless": False, "incognito": True,
        "test": True, "locale": "en", "ad_block": True,
        "undetectable": True, "multi_proxy": True,
        "chromium_arg": ",".join(chromium_args),
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

    try:
        with SB(**sb_kwargs) as sb:
            sb.driver.set_page_load_timeout(45)
            q = " ".join([ROW["business_name"], ROW["business_city"], ROW["business_state"]])
            url = f"https://www.google.com/maps/search/{quote_plus(q)}?hl=en"
            print(f"GET {url}")
            sb.uc_open_with_reconnect(url, reconnect_time=8)
            time.sleep(random.uniform(5, 8))
            print(f"  current_url: {sb.get_current_url()[:120]}")

            # Try captcha solver (no-op if no captcha)
            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                print(f"  uc_gui_click_captcha: {type(e).__name__}: {e}")

            # Save pre-click DOM
            with open(os.path.join(OUT_DIR, "0_initial.html"), "w") as f:
                f.write(sb.get_page_source() or "")

            # If we're on /maps/search/ click the first card
            cur = sb.get_current_url() or ""
            if "/maps/place/" not in cur:
                try:
                    cards = sb.find_elements("css selector", "a.hfpxzc")
                    if cards:
                        cards[0].click()
                        time.sleep(random.uniform(2.5, 4.0))
                        cur = sb.get_current_url() or ""
                        print(f"  after-click: {cur[:120]}")
                except Exception as e:
                    print(f"  card click error: {e}")

            with open(os.path.join(OUT_DIR, "1_place_loaded.html"), "w") as f:
                f.write(sb.get_page_source() or "")

            # === REVIEWS TAB CLICK — try every strategy and report ===
            print("\n=== Reviews tab strategies ===")
            try:
                tabs = sb.find_elements("css selector", "button[role='tab']")
                print(f"  button[role='tab'] count: {len(tabs)}")
                for i, t in enumerate(tabs):
                    lbl = t.get_attribute("aria-label") or ""
                    txt = (t.text or "").strip()
                    print(f"    tab[{i}] aria='{lbl[:60]}' text='{txt[:30]}'")
            except Exception as e:
                print(f"  list tabs error: {e}")

            clicked = False
            # Strategy A: aria-label includes "Reviews"
            try:
                els = sb.find_elements("css selector", 'button[role="tab"][aria-label*="Reviews"]')
                if els:
                    sb.execute_script("arguments[0].click();", els[0])
                    clicked = True
                    print(f"  ✓ Strategy A clicked: aria-label*='Reviews' ({len(els)} matched)")
            except Exception as e:
                print(f"  Strategy A error: {e}")

            # Strategy B: tab text == "Reviews"
            if not clicked:
                try:
                    els = sb.find_elements("xpath",
                        "//button[@role='tab'][normalize-space(.)='Reviews']")
                    if els:
                        sb.execute_script("arguments[0].click();", els[0])
                        clicked = True
                        print(f"  ✓ Strategy B clicked: button text='Reviews'")
                except Exception as e:
                    print(f"  Strategy B error: {e}")

            # Strategy C: 2nd tab (positional fallback)
            if not clicked:
                try:
                    tabs = sb.find_elements("css selector", "button[role='tab']")
                    if len(tabs) >= 2:
                        sb.execute_script("arguments[0].click();", tabs[1])
                        clicked = True
                        print(f"  ✓ Strategy C clicked: 2nd tab (positional)")
                except Exception as e:
                    print(f"  Strategy C error: {e}")

            time.sleep(random.uniform(3, 5))
            with open(os.path.join(OUT_DIR, "2_after_reviews_click.html"), "w") as f:
                f.write(sb.get_page_source() or "")

            # Scroll the reviews pane to load more
            print("\n=== Scrolling reviews pane ===")
            for i in range(30):
                h = sb.execute_script("""
                    var ds = document.querySelectorAll('div[role="main"] div.m6QErb');
                    var target = null;
                    for (var i=0; i<ds.length; i++) {
                        if (ds[i].scrollHeight > ds[i].clientHeight + 50) {
                            target = ds[i];
                        }
                    }
                    if (target) target.scrollBy(0, 3000);
                    return target ? target.scrollHeight : 0;
                """)
                if i % 5 == 0:
                    print(f"  scroll {i}: scrollHeight={h}")
                time.sleep(random.uniform(0.7, 1.1))

            with open(os.path.join(OUT_DIR, "3_after_scroll.html"), "w") as f:
                f.write(sb.get_page_source() or "")

            # Count what we got
            try:
                cards = sb.find_elements("css selector", "div.jftiEf")
                print(f"\n  div.jftiEf count after scroll: {len(cards)}")
                cards2 = sb.find_elements("css selector", "div[data-review-id]")
                print(f"  div[data-review-id] count: {len(cards2)}")
                cards3 = sb.find_elements("css selector", "div[jsaction*='reviewerLink']")
                print(f"  div[jsaction*='reviewerLink']: {len(cards3)}")
            except Exception as e:
                print(f"  count error: {e}")
    finally:
        if bridge:
            try: bridge.stop()
            except: pass
        if disp:
            try: disp.stop()
            except: pass

    print(f"\nSaved to {OUT_DIR}/")
    for fn in sorted(os.listdir(OUT_DIR)):
        full = os.path.join(OUT_DIR, fn)
        if os.path.isfile(full):
            print(f"  {fn:30s}  {os.path.getsize(full):>10d} bytes")


if __name__ == "__main__":
    main()
