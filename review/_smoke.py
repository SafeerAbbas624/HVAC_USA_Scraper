"""Single-scraper smoke test — exercises one site end-to-end without the
multi-process retry harness.  Useful for fast iteration on selectors.

Usage:
    python -m review._smoke bbb         # rows 25-29
    python -m review._smoke angi --row 26
"""
import argparse
import os
import sys
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_BASE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from seleniumbase import SB
from socks_bridge import Socks5Bridge
from review import csv_io
from review import config as rconfig
from review import proxies as proxies_mod
from review.sites import get_scraper


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("site", choices=["yelp", "bbb", "angi", "homeadvisor",
                                          "google_maps"])
    parser.add_argument("--rows", type=str, default="25-29",
                        help="Row index range, e.g. '25-29' or '26'")
    args = parser.parse_args()

    if "-" in args.rows:
        start, end = [int(x) for x in args.rows.split("-")]
        idxs = list(range(start, end + 1))
    else:
        idxs = [int(args.rows)]

    _, rows = csv_io.read_input_csv(rconfig.INPUT_CSV)

    proxies = proxies_mod.load_proxies()
    proxy = proxies[0]
    bridge = Socks5Bridge(
        socks_host=proxy["ip"], socks_port=proxy["port"],
        socks_user=proxy.get("username"), socks_pass=proxy.get("password"),
    )
    port = bridge.start()
    proxy_str = f"127.0.0.1:{port}"

    scraper = get_scraper(args.site)
    print(f"Site: {args.site}  proxy: {proxy['scheme']}://{proxy['ip']}:{proxy['port']}")

    try:
        for ix in idxs:
            if ix >= len(rows):
                continue
            row = rows[ix]
            biz = (row.get("business_name") or "").strip()
            print(f"\n=== Row {ix}: '{biz}' / {row.get('business_city')}, {row.get('business_state')} ===")
            t0 = time.time()
            with make_sb(proxy_str) as sb:
                try:
                    profile = scraper.search(sb, row)
                    print(f"  search → {profile}")
                    if not profile:
                        print(f"  status: NOT_FOUND ({time.time()-t0:.1f}s)")
                        continue
                    data = scraper.scrape(sb, profile)
                except Exception as e:
                    print(f"  RAISED: {type(e).__name__}: {e}")
                    continue
            rating = data.get("rating")
            n_reviews = len(data.get("reviews") or [])
            print(f"  rating={rating}  reviews={n_reviews}  ({time.time()-t0:.1f}s)")
            if data.get("reviews"):
                r = data["reviews"][0]
                print(f"  first review: author='{r.get('author','')}' "
                      f"rating={r.get('rating')} date='{r.get('date','')[:20]}' "
                      f"text='{(r.get('text','') or '')[:80]}...'")
    finally:
        bridge.stop()


if __name__ == "__main__":
    sys.exit(main() or 0)
