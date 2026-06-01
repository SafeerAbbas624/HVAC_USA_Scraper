"""Inspect harvested HTML to find real selectors per site.

For each site's search.html, lists distinct href patterns + any classes/ids
containing 'rating'/'review'/'stars'/'profile'/'biz'/'listing'.
For each profile.html, extracts likely rating containers + review card containers.

Usage:
  python -m review._inspect                # all sites
  python -m review._inspect bbb angi       # only specific
"""
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

from bs4 import BeautifulSoup

HARVEST_DIR = os.path.join(os.path.dirname(__file__), "_harvest")


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _hosts_of_links(soup, must_include=None, must_exclude=None):
    """Return Counter of (host, path-prefix) tuples for all anchor hrefs."""
    cnt = Counter()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href: continue
        if must_include and must_include not in href: continue
        if must_exclude and any(x in href for x in must_exclude): continue
        try:
            p = urlparse(href if "://" in href else "http://example.com" + href)
            host = p.netloc.replace("www.", "")
            path_seg = p.path.split("/")[1:3]
            key = f"{host}/{'/'.join(s for s in path_seg if s)[:40]}"
            cnt[key] += 1
        except Exception: continue
    return cnt


def _classes_with(soup, keyword):
    """Return Counter of class names containing the keyword."""
    cnt = Counter()
    for el in soup.find_all(class_=True):
        for cls in el.get("class", []):
            if keyword.lower() in cls.lower():
                cnt[cls] += 1
    return cnt


def _ids_with(soup, keyword):
    cnt = Counter()
    for el in soup.find_all(id=True):
        if keyword.lower() in el.get("id", "").lower():
            cnt[el.get("id")] += 1
    return cnt


def _testids_with(soup, keyword):
    cnt = Counter()
    for el in soup.find_all(attrs={"data-testid": True}):
        v = el.get("data-testid", "")
        if keyword.lower() in v.lower():
            cnt[v] += 1
    return cnt


def _itemprops(soup):
    cnt = Counter()
    for el in soup.find_all(attrs={"itemprop": True}):
        cnt[el.get("itemprop")] += 1
    return cnt


def inspect_search(site_id):
    """Find profile-link patterns in search.html."""
    path = os.path.join(HARVEST_DIR, site_id, "search.html")
    html = _load(path)
    if not html:
        print(f"  no search.html"); return
    print(f"  search.html size: {len(html)}")
    soup = BeautifulSoup(html, "html.parser")
    # All href hosts + path prefixes (top 15)
    site_hosts = _hosts_of_links(soup, must_exclude=["doubleclick", "googletagmanager", "googleadservices",
                                                      "googletagservices", "facebook.com", "tiktok.com",
                                                      "twitter.com", "reddit", "instagram"])
    print("  top href patterns (first segment):")
    for k, v in site_hosts.most_common(12):
        print(f"    [{v:>3d}]  {k}")


def inspect_profile(site_id):
    """Find rating + review-card patterns in profile.html."""
    for fname in ("profile.html", "profile_fallback.html"):
        path = os.path.join(HARVEST_DIR, site_id, fname)
        html = _load(path)
        if not html: continue
        print(f"\n  -- {fname} ({len(html)} bytes) --")
        soup = BeautifulSoup(html, "html.parser")
        for kw in ("rating", "stars", "score", "review"):
            cls = _classes_with(soup, kw)
            if cls:
                print(f"    classes containing '{kw}': "
                      + ", ".join(f"{c}({n})" for c, n in cls.most_common(8)))
        tids = _testids_with(soup, "rating") + _testids_with(soup, "review") + _testids_with(soup, "star")
        if tids:
            print(f"    data-testid containing rating/review/star: "
                  + ", ".join(f"{c}({n})" for c, n in tids.most_common(8)))
        ips = _itemprops(soup)
        if ips:
            print(f"    itemprops present: "
                  + ", ".join(f"{c}({n})" for c, n in ips.most_common(8)))
        # Look for explicit rating/star numbers near visible text
        # Find all aria-labels containing star/rating
        ar = Counter()
        for el in soup.find_all(attrs={"aria-label": True}):
            v = el.get("aria-label", "")
            if any(s in v.lower() for s in ("star", "rating", "out of 5", "/5")):
                ar[v[:80]] += 1
        if ar:
            print(f"    aria-labels with star/rating (top 5):")
            for k, v in ar.most_common(5):
                print(f"      [{v}]  {k}")


def main(argv=None):
    sites = argv or sorted(os.listdir(HARVEST_DIR))
    for site_id in sites:
        if site_id.startswith("."): continue
        d = os.path.join(HARVEST_DIR, site_id)
        if not os.path.isdir(d): continue
        print(f"\n========== {site_id} ==========")
        inspect_search(site_id)
        inspect_profile(site_id)


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else None)
