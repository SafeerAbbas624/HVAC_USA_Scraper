"""Yelp Academic Open Dataset loader.

Source: archive.org mirror of the official Yelp dataset (160K businesses, 6.99M
reviews, sampled from MA/OR/TX/FL/GA/BC/OH/CO/WA primarily).

Files expected (in /var/HVAC/review/yelp_dataset/):
  yelp_academic_dataset_business.json.gz   ~21 MB compressed, ~150 MB raw
  yelp_academic_dataset_review.json.gz     ~2.9 GB compressed, ~7 GB raw

Pipeline:
  1. build_business_index() — load business.json once, build an in-memory
     dict keyed by (state | slug(city) | normalized_name) → business record.
     Saved as JSON to yelp_business_index.json so subsequent runs are fast.
  2. lookup_business(name, city, state) — exact key lookup, then token-overlap
     fuzzy fallback (≥0.5 Jaccard threshold) within the same (state, city).
  3. build_review_cache(business_ids) — stream-filter review.json.gz, keeping
     only reviews for the supplied biz IDs. One-time cost: ~10 minutes.
  4. get_yelp_data(name, city, state) — convenience wrapper used by the
     yelp.py scraper.

The review file is ~7 GB raw so we never load it into memory — we stream
line by line. Once the small per-business slice is cached, lookups are O(1).
"""
import gzip
import json
import logging
import os
import re
import sys
import threading
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("review")

DATASET_DIR = Path(__file__).parent / "yelp_dataset"
BUSINESS_GZ = DATASET_DIR / "yelp_academic_dataset_business.json.gz"
BUSINESS_JSON = DATASET_DIR / "yelp_academic_dataset_business.json"  # uncompressed alt
REVIEW_GZ = DATASET_DIR / "yelp_academic_dataset_review.json.gz"
REVIEW_JSON = DATASET_DIR / "yelp_academic_dataset_review.json"

INDEX_FILE = DATASET_DIR / "yelp_business_index.json"
REVIEWS_CACHE = DATASET_DIR / "yelp_reviews_cache.json"
# Compact lookup file: maps row-key → {rating, review_count, reviews}.
# Built once via `_build_for_csv()`. Loaded by the yelp scraper, ~MB instead
# of the 44MB business index, so per-subprocess load is ~50ms not 5s.
MATCHES_FILE = DATASET_DIR / "yelp_matches.json"

_FUZZY_THRESHOLD = 0.7

# Tokens common across HVAC business names — they shouldn't drive a match.
# If two names overlap ONLY on these, we reject the match.
_HVAC_STOP_TOKENS = frozenset({
    "heating", "cooling", "air", "conditioning", "ac", "hvac", "service",
    "services", "company", "of", "the", "and", "co", "inc", "llc", "corp",
    "plumbing", "electrical", "electric", "mechanical", "refrigeration",
    "north", "south", "east", "west", "central", "metro",
})

_index_lock = threading.RLock()  # reentrant: get_state_city_idx -> build_index
_index_cached = None       # flat key->[biz] index (kept for compatibility)
_state_city_idx = None     # state -> city_slug -> [biz...]   for fast scan
_reviews_cached = None


# --------------------------------------------------------------- normalization

def slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s


_LEGAL_RX = re.compile(
    r"\b(llc|inc|incorporated|corp|corporation|company|co|ltd|limited|"
    r"plc|services?|svc|svcs|the|and)\b\.?", re.I,
)


def normalize_name(name: str) -> str:
    """Tokenization-friendly: drop punctuation, drop legal suffixes,
    map '&' to 'and', collapse whitespace, lowercase."""
    s = (name or "").replace("&", " and ")
    s = re.sub(r"[,\.\(\)\"'\\\\:]", " ", s)
    s = _LEGAL_RX.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# ---------------------------------------------------------------- file readers

def _open_jsonl(path: Path):
    """Open .json or .json.gz, yield parsed records line by line."""
    p = Path(path)
    if str(p).endswith(".gz"):
        f = gzip.open(p, "rt", encoding="utf-8")
    else:
        f = open(p, "r", encoding="utf-8")
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        f.close()


def _resolve_business_path():
    if BUSINESS_GZ.exists():
        return BUSINESS_GZ
    if BUSINESS_JSON.exists():
        return BUSINESS_JSON
    return None


def _resolve_review_path():
    if REVIEW_GZ.exists():
        return REVIEW_GZ
    if REVIEW_JSON.exists():
        return REVIEW_JSON
    return None


# ---------------------------------------------------------------- index build

def build_business_index(force: bool = False) -> dict:
    """Returns {key: [business_dict, ...]}. Key = STATE|city-slug|name-normalized."""
    global _index_cached
    if _index_cached is not None and not force:
        return _index_cached
    with _index_lock:
        if _index_cached is not None and not force:
            return _index_cached
        if INDEX_FILE.exists() and not force:
            try:
                _index_cached = json.loads(INDEX_FILE.read_text())
                return _index_cached
            except Exception:
                logger.warning("yelp_business_index.json corrupt — rebuilding")

        path = _resolve_business_path()
        if not path:
            logger.warning(
                "Yelp dataset not found (expected at %s). "
                "Yelp scraper will return NOT_FOUND for all rows.",
                BUSINESS_GZ,
            )
            _index_cached = {}
            return _index_cached

        logger.info(f"Building Yelp business index from {path.name} (one-time)…")
        by_key = defaultdict(list)
        n = 0
        for biz in _open_jsonl(path):
            n += 1
            state = (biz.get("state") or "").upper()
            city = biz.get("city") or ""
            name = biz.get("name") or ""
            if not state or not city or not name:
                continue
            key = f"{state}|{slug(city)}|{normalize_name(name)}"
            by_key[key].append({
                "business_id": biz.get("business_id"),
                "name": name,
                "address": biz.get("address", ""),
                "city": city,
                "state": state,
                "stars": biz.get("stars"),
                "review_count": biz.get("review_count"),
                "categories": biz.get("categories", ""),
            })
        index = dict(by_key)
        INDEX_FILE.write_text(json.dumps(index))
        logger.info(
            f"Built Yelp business index: {n} businesses → "
            f"{len(index)} unique state|city|name keys"
        )
        _index_cached = index
        return _index_cached


# ---------------------------------------------------------------- lookup

def _fuzzy_score(a_tokens: set, b_tokens: set) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = a_tokens & b_tokens
    # Reject matches that overlap only on HVAC stop tokens
    distinguishing = overlap - _HVAC_STOP_TOKENS
    if not distinguishing:
        return 0.0
    return len(overlap) / max(len(a_tokens | b_tokens), 1)


def _get_state_city_idx():
    """Build (or reuse) state -> city_slug -> [biz, ...] hierarchical index."""
    global _state_city_idx
    if _state_city_idx is not None:
        return _state_city_idx
    with _index_lock:
        if _state_city_idx is not None:
            return _state_city_idx
        flat = build_business_index()
        if not flat:
            _state_city_idx = {}
            return _state_city_idx
        sci = defaultdict(lambda: defaultdict(list))
        for key, businesses in flat.items():
            try:
                state, city_slug, _name_n = key.split("|", 2)
            except ValueError:
                continue
            for b in businesses:
                # Pre-compute and cache the name token set for fast lookups
                if "_name_tokens" not in b:
                    b["_name_tokens"] = set(
                        re.findall(r"\w+", normalize_name(b["name"]))
                    )
                sci[state][city_slug].append(b)
        _state_city_idx = {s: dict(c) for s, c in sci.items()}
        return _state_city_idx


def lookup_business(name: str, city: str, state: str, index=None):
    """Exact lookup, then fuzzy match within same (state, city), then state-wide."""
    state = (state or "").upper()
    city_s = slug(city or "")
    name_n = normalize_name(name or "")
    if not name_n:
        return None
    name_tokens = set(re.findall(r"\w+", name_n))
    if not name_tokens:
        return None

    sci = _get_state_city_idx()
    state_block = sci.get(state)
    if not state_block:
        return None

    # 1) Exact (state, city, name) via flat index
    if index is None:
        index = build_business_index()
    candidates = index.get(f"{state}|{city_s}|{name_n}") or []
    if candidates:
        return candidates[0]

    # 2) Fuzzy within same (state, city) — only this city's businesses
    same_city = state_block.get(city_s) or []
    best = None
    best_score = 0.0
    for biz in same_city:
        sc = _fuzzy_score(name_tokens, biz["_name_tokens"])
        if sc > best_score:
            best_score = sc
            best = biz
    if best and best_score >= _FUZZY_THRESHOLD:
        return best

    return None


# ---------------------------------------------------------------- review cache

def build_review_cache(business_ids, force: bool = False) -> dict:
    """Stream review.json.gz once, retain only reviews for the requested IDs.

    Stored as {business_id: [review_dict, ...]} JSON file. Also kept in
    process memory for fast lookup during a run.
    """
    global _reviews_cached
    if _reviews_cached is not None and not force:
        return _reviews_cached

    with _index_lock:
        if _reviews_cached is not None and not force:
            return _reviews_cached

        if REVIEWS_CACHE.exists() and not force:
            try:
                _reviews_cached = json.loads(REVIEWS_CACHE.read_text())
                return _reviews_cached
            except Exception:
                logger.warning("yelp_reviews_cache.json corrupt — rebuilding")

        path = _resolve_review_path()
        if not path:
            logger.warning("Yelp review file missing — rating-only mode")
            _reviews_cached = {}
            return _reviews_cached

        bid_set = set(business_ids or [])
        logger.info(
            f"Streaming {path.name} ({path.stat().st_size/1e9:.2f} GB) "
            f"for {len(bid_set)} business IDs…"
        )
        by_biz = defaultdict(list)
        n = 0
        matched = 0
        for r in _open_jsonl(path):
            n += 1
            if n % 500000 == 0:
                logger.info(f"  scanned {n:,} reviews, {matched:,} matched")
            bid = r.get("business_id")
            if bid in bid_set:
                matched += 1
                by_biz[bid].append({
                    "author": "(Yelp user)",  # academic dataset anonymizes users
                    "rating": r.get("stars"),
                    "date": (r.get("date") or "")[:10],
                    "text": r.get("text", ""),
                })
        # Sort each business's reviews by date desc, then trim to 100
        for bid, revs in by_biz.items():
            revs.sort(key=lambda x: x.get("date") or "", reverse=True)
            del revs[100:]
        out = dict(by_biz)
        REVIEWS_CACHE.write_text(json.dumps(out))
        logger.info(
            f"Cached {sum(len(v) for v in out.values())} reviews "
            f"across {len(out)} businesses → {REVIEWS_CACHE.name}"
        )
        _reviews_cached = out
        return _reviews_cached


# ---------------------------------------------------------------- public API

def get_yelp_data(name: str, city: str, state: str):
    """Returns {rating, review_count, reviews, business_id, matched_name}
    or None if no match in the dataset."""
    biz = lookup_business(name, city, state)
    if not biz:
        return None
    reviews = []
    if REVIEWS_CACHE.exists():
        cache = build_review_cache(business_ids=[])  # uses cached file as-is
        reviews = cache.get(biz["business_id"], [])
    return {
        "rating": biz.get("stars"),
        "review_count": biz.get("review_count"),
        "reviews": reviews,
        "business_id": biz["business_id"],
        "matched_name": biz["name"],
    }


def is_available() -> bool:
    """True if the dataset is downloaded enough to attempt lookups."""
    return _resolve_business_path() is not None


# ---------------------------------------------------------------- CLI


def _stats():
    """Print dataset stats — useful to confirm download integrity."""
    bp = _resolve_business_path()
    rp = _resolve_review_path()
    print(f"Business file: {bp} (exists={bp is not None})")
    print(f"Review file:   {rp} (exists={rp is not None})")
    idx = build_business_index()
    print(f"Index entries: {len(idx)}")
    if idx:
        # Most common states
        from collections import Counter
        states = Counter()
        for k in idx.keys():
            states[k.split("|", 1)[0]] += 1
        print("Top 10 states:", states.most_common(10))


def _row_match_key(name: str, city: str, state: str) -> str:
    return f"{(state or '').upper()}|{slug(city or '')}|{normalize_name(name or '')}"


def _build_for_csv(csv_path: str):
    """Match a CSV of businesses to Yelp dataset, build review cache, and
    save a compact `yelp_matches.json` mapping row-key → result.

    The matches file is what the yelp.py scraper loads at runtime — it's
    ~MB rather than the 44MB index, so per-subprocess load is ~50ms.
    """
    import csv as csv_mod
    idx = build_business_index()
    if not idx:
        print("No index — cannot proceed")
        return

    matches_per_row = {}  # row_key -> business
    business_ids = set()
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        for row in reader:
            name = (row.get("business_name") or "").strip()
            city = (row.get("business_city") or "").strip()
            state = (row.get("business_state") or "").strip()
            if not name:
                continue
            biz = lookup_business(name, city, state, index=idx)
            if biz:
                key = _row_match_key(name, city, state)
                # Drop the cached _name_tokens before serializing
                clean_biz = {k: v for k, v in biz.items() if not k.startswith("_")}
                matches_per_row[key] = clean_biz
                business_ids.add(biz["business_id"])
    print(f"Matched {len(matches_per_row)} unique row-keys → "
          f"{len(business_ids)} unique businesses")

    # Stream review cache (one-time scan; reuses existing cache file
    # if present and the same biz IDs are already covered)
    reuse_cache = REVIEWS_CACHE.exists()
    if reuse_cache:
        try:
            existing = json.loads(REVIEWS_CACHE.read_text())
            covered = set(existing.keys())
            missing = business_ids - covered
            if not missing:
                print(f"Reusing existing {REVIEWS_CACHE.name}")
                reviews = existing
            else:
                print(f"Existing cache missing {len(missing)} biz IDs — rebuilding")
                reviews = build_review_cache(sorted(business_ids), force=True)
        except Exception:
            reviews = build_review_cache(sorted(business_ids), force=True)
    else:
        reviews = build_review_cache(sorted(business_ids), force=True)

    # Write matches with embedded review slices
    out = {}
    for key, biz in matches_per_row.items():
        bid = biz["business_id"]
        out[key] = {
            "rating": biz.get("stars"),
            "review_count": biz.get("review_count"),
            "reviews": reviews.get(bid, []),
            "business_id": bid,
            "matched_name": biz.get("name"),
        }
    MATCHES_FILE.write_text(json.dumps(out))
    sz_mb = MATCHES_FILE.stat().st_size / 1024 / 1024
    print(f"Wrote {MATCHES_FILE.name} ({sz_mb:.1f} MB) "
          f"with {len(out)} entries.")


def _load_matches_quick():
    """Cheap loader used by the yelp scraper at runtime."""
    if not MATCHES_FILE.exists():
        return None
    try:
        return json.loads(MATCHES_FILE.read_text())
    except Exception:
        return None


def get_yelp_data_fast(name: str, city: str, state: str):
    """O(1) lookup against pre-built yelp_matches.json (no index load)."""
    cache = _load_matches_quick()
    if cache is None:
        return None  # caller should fall back to get_yelp_data()
    return cache.get(_row_match_key(name, city, state))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        _stats()
    elif len(sys.argv) > 2 and sys.argv[1] == "build":
        _build_for_csv(sys.argv[2])
    else:
        print("Usage:")
        print("  python -m review.yelp_dataset_loader stats")
        print("  python -m review.yelp_dataset_loader build <csv_path>")
