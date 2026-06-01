"""Harvest real HTML from each review site so we can find the right selectors.

Runs discovery (search) for one well-known HVAC contractor per site, then if
discovery succeeds, navigates to the profile and saves the profile HTML.

Single Chrome session per site (sequential, NOT parallel) so we don't trigger
the proxy-hammer block pattern we saw in v3/v4.

Output:
  review/_harvest/<site_id>/search.html       — search-results page
  review/_harvest/<site_id>/profile.html      — profile/business page (if found)
  review/_harvest/<site_id>/profile_url.txt   — the URL we landed on
  review/_harvest/<site_id>/notes.txt         — what happened (success/blocked/etc.)

Usage:
  python -m review._harvest                  # all 17 sites
  python -m review._harvest yelp bbb         # only specific sites
"""
import logging
import os
import random
import sys
import time
from urllib.parse import quote_plus, urlparse

from . import config
from . import browser
from . import proxies as proxies_mod
from . import block_detect

config.setup_logging(log_file=os.path.join(os.path.dirname(__file__), "_harvest.log"))
logger = logging.getLogger("review")

HARVEST_DIR = os.path.join(os.path.dirname(__file__), "_harvest")

# A well-known national HVAC franchise that should be on every site
PRIMARY_ROW = {
    "business_name": "ARS / Rescue Rooter",
    "business_city": "Atlanta",
    "business_state": "GA",
    "website_url": "https://www.ars.com",
}
# Fallback: confirmed on BBB by earlier probe
FALLBACK_ROW = {
    "business_name": "205 Heating & Cooling LLC",
    "business_city": "Birmingham",
    "business_state": "AL",
    "website_url": "",
}


def _save(site_id: str, filename: str, content: str):
    path = os.path.join(HARVEST_DIR, site_id, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return path


def _settle(seconds_range=(3, 5)):
    time.sleep(random.uniform(*seconds_range))


# ---------------------------------------------------------------------------
# Per-site harvest functions: each returns dict of urls/html/notes
# ---------------------------------------------------------------------------

def harvest_google_maps(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    q = " ".join([name, city, state])
    url = f"https://www.google.com/maps/search/{quote_plus(q)}?hl=en"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle((5, 7))
    return {"search": sb.get_page_source(), "profile_url": sb.get_current_url()}


def harvest_yelp(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.yelp.com/search?find_desc={quote_plus(name)}&find_loc={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    if not block_detect.is_blocked(sb, "yelp.com"):
        try:
            anchors = sb.execute_script(
                "return Array.from(document.querySelectorAll('a[href*=\"/biz/\"]')).slice(0,3).map(a=>a.href);"
            ) or []
            for href in anchors:
                if "/biz/" in href:
                    sb.open(href.split("?")[0]); _settle()
                    out["profile_url"] = sb.get_current_url()
                    out["profile"] = sb.get_page_source()
                    break
        except Exception as e:
            out["error"] = str(e)
    return out


def harvest_bbb(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.bbb.org/search?find_text={quote_plus(name)}&find_loc={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/profile/\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/profile/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_angi(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.angi.com/search?searchKey={quote_plus(name)}&location={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll("
            "'a[href*=\"/companylist/\"], a[href*=\"/sp/\"], a[href*=\"/reviews/\"]'"
            ")).slice(0,5).map(a=>a.href);"
        ) or []
        for href in anchors:
            if any(k in href for k in ("/companylist/", "/sp/", "/reviews/")):
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_homeadvisor(sb, row):
    return _harvest_via_google(sb, row, "homeadvisor.com",
                               profile_keys=("/rated.", "/business/"))


def harvest_houzz(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.houzz.com/professionals/probr0--{quote_plus(name)}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/pro/\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/pro/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_thumbtack(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.thumbtack.com/search?q={quote_plus(name)}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll("
            "'a[href*=\"/services/\"], a[href*=\"/profile/\"]'"
            ")).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/services/" in href or "/profile/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_porch(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://porch.com/search?keyword={quote_plus(name)}&location={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/pp\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/pp" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_networx(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.networx.com/search?q={quote_plus(f'{name} {city} {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll("
            "'a[href*=\"/contractor/\"], a[href*=\"/profile/\"], a[href*=\"/pro/\"]'"
            ")).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if any(k in href for k in ("/contractor/", "/profile/", "/pro/")):
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_yellowpages(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.yellowpages.com/search?search_terms={quote_plus(name)}&geo_location_terms={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": sb.get_current_url(), "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll("
            "'a.business-name, a[class*=\"business-name\"], h3 a'"
            ")).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if href and "yellowpages.com" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_trustpilot(sb, row):
    web = row.get("website_url", "")
    if web:
        dom = urlparse(web if "://" in web else "http://" + web).netloc.lower()
        if dom.startswith("www."): dom = dom[4:]
        if dom:
            url = f"https://www.trustpilot.com/review/{dom}"
            sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
            return {"search": sb.get_page_source(), "search_url": url,
                    "profile_url": sb.get_current_url(),
                    "profile": sb.get_page_source()}
    name = row["business_name"]
    url = f"https://www.trustpilot.com/search?query={quote_plus(name)}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/review/\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/review/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_consumeraffairs(sb, row):
    name = row["business_name"]
    url = f"https://www.consumeraffairs.com/search?query={quote_plus(name)}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll("
            "'a[href*=\"/business/\"], a[href*=\"/online/\"], a[href*=\"/heating-cooling/\"]'"
            ")).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/business/" in href or "/online/" in href or "/heating-cooling/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_sitejabber(sb, row):
    web = row.get("website_url", "")
    if web:
        dom = urlparse(web if "://" in web else "http://" + web).netloc.lower()
        if dom.startswith("www."): dom = dom[4:]
        if dom:
            url = f"https://www.sitejabber.com/reviews/{dom}"
            sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
            return {"search": sb.get_page_source(), "search_url": url,
                    "profile_url": sb.get_current_url(),
                    "profile": sb.get_page_source()}
    name = row["business_name"]
    url = f"https://www.sitejabber.com/search?q={quote_plus(name)}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    return {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}


def harvest_manta(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.manta.com/mb_search?search={quote_plus(f'{name} {city} {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/c/\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/c/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_foursquare(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://foursquare.com/explore?q={quote_plus(name)}&near={quote_plus(f'{city}, {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}
    try:
        anchors = sb.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/v/\"]')).slice(0,3).map(a=>a.href);"
        ) or []
        for href in anchors:
            if "/v/" in href:
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


def harvest_mapquest(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.mapquest.com/search/results?query={quote_plus(f'{name} {city} {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    return {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}


def harvest_bing_places(sb, row):
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    url = f"https://www.bing.com/maps?q={quote_plus(f'{name} {city} {state}')}"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle((4, 6))
    return {"search": sb.get_page_source(), "search_url": url,
            "profile_url": sb.get_current_url(),
            "profile": sb.get_page_source()}


def _harvest_via_google(sb, row, target_domain, profile_keys):
    """Generic Google site:foo.com fallback."""
    name = row["business_name"]; city = row["business_city"]; state = row["business_state"]
    q = f'site:{target_domain} "{name}" {city} {state}'
    url = f"https://www.google.com/search?q={quote_plus(q)}&hl=en&num=20"
    sb.uc_open_with_reconnect(url, reconnect_time=8); _settle()
    out = {"search": sb.get_page_source(), "search_url": url, "profile_url": None, "profile": None}
    try:
        hrefs = sb.execute_script(
            f"return Array.from(document.querySelectorAll('a[href]'))"
            f".map(a => a.href).filter(h => h.includes('{target_domain}')).slice(0,5);"
        ) or []
        for href in hrefs:
            if any(k in href for k in profile_keys):
                sb.open(href.split("?")[0]); _settle()
                out["profile_url"] = sb.get_current_url()
                out["profile"] = sb.get_page_source()
                break
    except Exception as e:
        out["error"] = str(e)
    return out


HARVESTERS = {
    "google_maps": harvest_google_maps,
    "yelp": harvest_yelp,
    "bbb": harvest_bbb,
    "angi": harvest_angi,
    "homeadvisor": harvest_homeadvisor,
    "houzz": harvest_houzz,
    "thumbtack": harvest_thumbtack,
    "porch": harvest_porch,
    "networx": harvest_networx,
    "yellowpages": harvest_yellowpages,
    "trustpilot": harvest_trustpilot,
    "consumeraffairs": harvest_consumeraffairs,
    "sitejabber": harvest_sitejabber,
    "manta": harvest_manta,
    "foursquare": harvest_foursquare,
    "mapquest": harvest_mapquest,
    "bing_places": harvest_bing_places,
}


def _open_chrome(proxy):
    proxy_str, bridge = browser._build_proxy_string(proxy)
    from seleniumbase import SB
    chromium_args = [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-blink-features=AutomationControlled", "--mute-audio",
    ]
    sb_kwargs = {"uc": True, "headless": False, "incognito": True,
                 "chromium_arg": ",".join(chromium_args)}
    if browser._CHROME_BINARY:
        sb_kwargs["binary_location"] = browser._CHROME_BINARY
    if proxy_str:
        sb_kwargs["proxy"] = proxy_str
    return SB, sb_kwargs, bridge


def main(argv=None):
    sites = argv or list(HARVESTERS.keys())
    proxies = proxies_mod.load_proxies()
    print(f"Loaded {len(proxies)} proxies")

    try:
        from sbvirtualdisplay import Display
        disp = Display(visible=0, size=(1920, 1080))
        disp.start()
    except Exception:
        disp = None

    for site_id in sites:
        if site_id not in HARVESTERS:
            print(f"  [{site_id}] unknown — skip")
            continue
        print(f"\n=== Harvesting {site_id} ===")
        proxy = proxies_mod.pick_proxy(proxies)
        SB, sb_kwargs, bridge = _open_chrome(proxy)
        notes = []
        try:
            with SB(**sb_kwargs) as sb:
                sb.driver.set_page_load_timeout(45)
                sb.driver.set_script_timeout(45)
                # Try primary, then fallback if discovery failed
                for label, row in (("primary", PRIMARY_ROW), ("fallback", FALLBACK_ROW)):
                    try:
                        result = HARVESTERS[site_id](sb, row)
                    except Exception as e:
                        notes.append(f"  {label} {row['business_name']!r}: EXCEPTION {type(e).__name__}: {e}")
                        continue
                    blocked = block_detect.is_blocked(sb, "")
                    notes.append(
                        f"  {label} {row['business_name']!r}: "
                        f"search_size={len(result.get('search') or '')} "
                        f"profile_url={result.get('profile_url')} "
                        f"blocked={blocked} "
                        f"err={result.get('error','')}"
                    )
                    suffix = "" if label == "primary" else f"_{label}"
                    _save(site_id, f"search{suffix}.html", result.get("search") or "")
                    if result.get("profile"):
                        _save(site_id, f"profile{suffix}.html", result["profile"])
                        _save(site_id, f"profile_url{suffix}.txt", result["profile_url"] or "")
                    # If primary got profile, no need for fallback
                    if label == "primary" and result.get("profile"):
                        break
        finally:
            if bridge:
                try: bridge.stop()
                except: pass
        _save(site_id, "notes.txt", "\n".join(notes))
        for line in notes:
            print(line)

    if disp:
        try: disp.stop()
        except: pass
    print(f"\nHarvest complete. Files in {HARVEST_DIR}/")


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else None)
