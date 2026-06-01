"""Test deep-link URL to force Google Maps reviews tab."""
import os
import random
import re
import time
from urllib.parse import quote_plus

from . import config
from . import browser
from . import proxies as proxies_mod

config.setup_logging()

# Use Degree Heating (the row that failed in v6)
ROW = {
    "business_name": "Degree Heating & Cooling",
    "business_city": "Waterbury",
    "business_state": "CT",
}

OUT = os.path.join(os.path.dirname(__file__), "_harvest", "google_maps_r20_v2")
os.makedirs(OUT, exist_ok=True)


def main():
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

    try:
        with SB(**sb_kwargs) as sb:
            sb.driver.set_page_load_timeout(45)
            q = " ".join([ROW["business_name"], ROW["business_city"], ROW["business_state"]])

            # === Try 1: /maps/search/ then take URL we land on, append !9m1!1b1 ===
            url1 = f"https://www.google.com/maps/search/{quote_plus(q)}?hl=en"
            print(f"\n=== Try 1: standard /maps/search/ ===\nGET {url1}")
            sb.uc_open_with_reconnect(url1, reconnect_time=8)
            time.sleep(random.uniform(5, 7))
            cur = sb.get_current_url() or ""
            print(f"  landed: {cur[:130]}")
            with open(os.path.join(OUT, "try1_landed.html"), "w") as f:
                f.write(sb.get_page_source() or "")
            tabs1 = sb.find_elements("css selector", "button[role='tab']")
            cards1 = sb.find_elements("css selector", "div.jftiEf")
            print(f"  tabs={len(tabs1)} jftiEf={len(cards1)}")

            # === Try 2: append !9m1!1b1 to force reviews tab ===
            if "/maps/place/" in cur and "data=" in cur:
                # The data param needs 9m1!1b1 appended
                if "!9m1!1b1" not in cur:
                    cur_with_reviews = re.sub(r"(data=[^?#]+)", r"\1!9m1!1b1", cur)
                else:
                    cur_with_reviews = cur
                print(f"\n=== Try 2: deep-link to reviews tab ===\nGET {cur_with_reviews[:130]}")
                sb.open(cur_with_reviews)
                time.sleep(random.uniform(4, 6))
                with open(os.path.join(OUT, "try2_data_9m1.html"), "w") as f:
                    f.write(sb.get_page_source() or "")
                tabs2 = sb.find_elements("css selector", "button[role='tab']")
                cards2 = sb.find_elements("css selector", "div.jftiEf")
                print(f"  tabs={len(tabs2)} jftiEf={len(cards2)}")

            # === Try 3: Google search (knowledge panel) ===
            url3 = f"https://www.google.com/search?q={quote_plus(q)}+reviews&hl=en"
            print(f"\n=== Try 3: google.com/search?q=... ===\nGET {url3}")
            sb.uc_open_with_reconnect(url3, reconnect_time=8)
            time.sleep(random.uniform(4, 6))
            with open(os.path.join(OUT, "try3_search.html"), "w") as f:
                f.write(sb.get_page_source() or "")
            print(f"  current: {sb.get_current_url()[:130]}")
            # Click 'View all reviews' if present
            try:
                # Possible selectors for 'reviews' link/button
                reviews_buttons = sb.find_elements("xpath",
                    "//a[contains(., 'reviews')] | //a[contains(., 'Reviews')]")
                print(f"  reviews-link count: {len(reviews_buttons)}")
                for b in reviews_buttons[:5]:
                    print(f"    {b.tag_name} '{b.text[:50]}' href={b.get_attribute('href')[:80] if b.get_attribute('href') else ''}")
            except Exception as e:
                print(f"  err: {e}")

    finally:
        if bridge:
            try: bridge.stop()
            except: pass
        if disp:
            try: disp.stop()
            except: pass

    print(f"\nFiles in {OUT}/")
    for fn in sorted(os.listdir(OUT)):
        full = os.path.join(OUT, fn)
        if os.path.isfile(full):
            print(f"  {fn:30s}  {os.path.getsize(full):>10d} bytes")


if __name__ == "__main__":
    main()
