"""HTML inspection harness — uses SB CDP Mode (Chrome DevTools Protocol).

CDP mode is the most undetectable SeleniumBase mode — bypasses many bot
detection systems (DataDome, PerimeterX, Cloudflare) that classic UC fails.

Usage:
    python -m review._inspect_sites yelp
    python -m review._inspect_sites all
"""
import argparse
import os
import re
import sys
import time
import random
from urllib.parse import quote_plus

_BASE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_BASE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from seleniumbase import SB
from socks_bridge import Socks5Bridge
from review import proxies as proxies_mod

DUMP_DIR = os.path.join(_BASE, "_inspect_out")
os.makedirs(DUMP_DIR, exist_ok=True)

ROW = {
    "business_name": "24 Hour Heating & Air Conditioning",
    "business_city": "Casper",
    "business_state": "WY",
    "website_url": "https://24hr-hvac.com",
}


def slug(s):
    s = re.sub(r"[^\w\s-]", "", s.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s


def dump(name, html):
    p = os.path.join(DUMP_DIR, name + ".html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  → wrote {p} ({len(html)} bytes)")
    return p


def is_blocked(html):
    low = (html or "").lower()
    for sig in ("captcha", "datadome", "perimeterx", "cloudflare-static",
                "verify you are human", "are you a human", "px-captcha",
                "request unsuccessful", "access denied", "just a moment"):
        if sig in low:
            return sig
    return None


def make_sb(proxy_str):
    chromium_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--mute-audio",
    ]
    return SB(
        uc=True,
        undetectable=True,
        headless=False,
        incognito=True,
        locale="en",
        chromium_arg=",".join(chromium_args),
        proxy=proxy_str,
        xvfb=True,
    )


# -------------------------------------------------------------------- YELP

def inspect_yelp(sb):
    print("\n=== YELP ===")
    name = ROW["business_name"]
    city = ROW["business_city"]
    state = ROW["business_state"]

    # Approach: CDP mode
    print("  activating CDP mode")
    sb.activate_cdp_mode("https://www.yelp.com/")
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  yelp homepage: size={len(html)} blocked={is_blocked(html)}")
    dump("yelp_cdp_home", html)

    # Try Yelp /search via CDP
    url = (f"https://www.yelp.com/search?"
           f"find_desc={quote_plus(name)}"
           f"&find_loc={quote_plus(f'{city}, {state}')}")
    print(f"  GET {url}")
    sb.cdp.open(url)
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  yelp /search: size={len(html)} blocked={is_blocked(html)}")
    dump("yelp_cdp_search", html)

    # Try direct biz URL guess
    guess = f"https://www.yelp.com/biz/{slug(name)}-{slug(city)}"
    print(f"  GET guess {guess}")
    sb.cdp.open(guess)
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  yelp guess: size={len(html)} blocked={is_blocked(html)}")
    dump("yelp_cdp_guess", html)


# -------------------------------------------------------------------- BBB

def inspect_bbb(sb):
    print("\n=== BBB ===")
    name = ROW["business_name"]
    city = ROW["business_city"]
    state = ROW["business_state"]

    print("  activating CDP mode")
    sb.activate_cdp_mode("https://www.bbb.org/")
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  bbb home: size={len(html)} blocked={is_blocked(html)}")
    dump("bbb_cdp_home", html)

    url = (f"https://www.bbb.org/search?"
           f"find_text={quote_plus(name)}"
           f"&find_loc={quote_plus(f'{city}, {state}')}")
    print(f"  GET {url}")
    sb.cdp.open(url)
    time.sleep(random.uniform(5, 7))
    html = sb.cdp.get_page_source()
    print(f"  bbb /search: size={len(html)} blocked={is_blocked(html)}")
    dump("bbb_cdp_search", html)

    try:
        hrefs = sb.cdp.evaluate(
            "Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.getAttribute('href'))"
            ".filter(h => h && /\\/profile\\//.test(h))"
            ".slice(0, 12)"
        )
        print(f"  bbb profile candidates: {hrefs}")
        prof = None
        for h in (hrefs or []):
            if re.match(r"^/(us|ca)/[a-z]{2,3}/[\w-]+/profile/[\w-]+/", h or "", re.I):
                prof = "https://www.bbb.org" + h
                break
        if prof:
            sb.cdp.open(prof)
            time.sleep(random.uniform(5, 7))
            html = sb.cdp.get_page_source()
            print(f"  bbb profile: size={len(html)} blocked={is_blocked(html)} url={sb.cdp.get_current_url()}")
            dump("bbb_cdp_profile", html)
    except Exception as e:
        print(f"  err: {e}")


# -------------------------------------------------------------------- ANGI

def inspect_angi(sb):
    print("\n=== ANGI ===")
    name = ROW["business_name"]
    city = ROW["business_city"]
    state = ROW["business_state"]

    print("  activating CDP mode")
    sb.activate_cdp_mode("https://www.angi.com/")
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  angi home: size={len(html)} blocked={is_blocked(html)}")
    dump("angi_cdp_home", html)

    url = (f"https://www.angi.com/search?"
           f"searchKey={quote_plus(name)}"
           f"&location={quote_plus(f'{city}, {state}')}")
    print(f"  GET {url}")
    sb.cdp.open(url)
    time.sleep(random.uniform(5, 7))
    html = sb.cdp.get_page_source()
    print(f"  angi /search: size={len(html)} blocked={is_blocked(html)}")
    dump("angi_cdp_search", html)
    try:
        hrefs = sb.cdp.evaluate(
            "Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
            ".filter(h => /\\/companylist\\//.test(h) || /\\/sp\\//.test(h)"
            "       || /\\/business\\//.test(h) || /\\/reviews\\//.test(h))"
            ".slice(0, 12)"
        )
        print(f"  angi candidates: {hrefs}")
    except Exception as e:
        print(f"  err: {e}")


# -------------------------------------------------------------------- HA

def inspect_homeadvisor(sb):
    print("\n=== HOMEADVISOR ===")
    name = ROW["business_name"]
    city = ROW["business_city"]
    state = ROW["business_state"]

    print("  activating CDP mode")
    sb.activate_cdp_mode("https://www.homeadvisor.com/")
    time.sleep(random.uniform(4, 6))
    html = sb.cdp.get_page_source()
    print(f"  ha home: size={len(html)} blocked={is_blocked(html)}")
    dump("ha_cdp_home", html)

    url = (f"https://www.homeadvisor.com/c.{quote_plus(name)}"
           f".{quote_plus(city)}.{quote_plus(state)}.html")
    print(f"  GET {url}")
    sb.cdp.open(url)
    time.sleep(random.uniform(5, 7))
    html = sb.cdp.get_page_source()
    print(f"  ha search: size={len(html)} blocked={is_blocked(html)}")
    dump("ha_cdp_search", html)
    print(f"  current url: {sb.cdp.get_current_url()}")
    try:
        hrefs = sb.cdp.evaluate(
            "Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
            ".filter(h => /\\/rated\\./.test(h) || /\\/business\\//.test(h) || /\\/sp\\//.test(h))"
            ".slice(0, 12)"
        )
        print(f"  ha candidates: {hrefs}")
    except Exception as e:
        print(f"  err: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sites", nargs="+",
                        help="yelp bbb angi homeadvisor all")
    parser.add_argument("--row", type=int, default=None,
                        help="Override ROW with input CSV row index")
    args = parser.parse_args()

    if args.row is not None:
        from review import csv_io
        from review import config as rconfig
        _, rows = csv_io.read_input_csv(rconfig.INPUT_CSV)
        if 0 <= args.row < len(rows):
            r = rows[args.row]
            global ROW
            ROW = {
                "business_name": r.get("business_name", "").strip(),
                "business_city": r.get("business_city", "").strip(),
                "business_state": r.get("business_state", "").strip(),
                "website_url": r.get("website_url", "").strip(),
            }
            print(f"Row {args.row}: {ROW}")

    targets = args.sites
    if "all" in targets:
        targets = ["yelp", "bbb", "angi", "homeadvisor"]

    proxies = proxies_mod.load_proxies()
    if not proxies:
        print("ERROR: no proxies in proxies.txt")
        return 1
    proxy = proxies[0]
    print(f"Proxy: {proxy['scheme']}://{proxy['ip']}:{proxy['port']}")

    bridge = None
    if proxy.get("scheme", "").startswith("socks"):
        bridge = Socks5Bridge(
            socks_host=proxy["ip"], socks_port=proxy["port"],
            socks_user=proxy.get("username"), socks_pass=proxy.get("password"),
        )
        port = bridge.start()
        proxy_str = f"127.0.0.1:{port}"
    else:
        auth = ""
        if proxy.get("username"):
            auth = f"{proxy['username']}:{proxy['password']}@"
        proxy_str = f"{auth}{proxy['ip']}:{proxy['port']}"

    handlers = {
        "yelp": inspect_yelp,
        "bbb": inspect_bbb,
        "angi": inspect_angi,
        "homeadvisor": inspect_homeadvisor,
    }

    try:
        for site in targets:
            with make_sb(proxy_str) as sb:
                handlers[site](sb)
    finally:
        if bridge:
            bridge.stop()

    print(f"\nAll dumps saved to {DUMP_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
