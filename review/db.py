"""PostgreSQL backend for the review scraper.

Source of truth (replacing the per-row CSV append):
    Database: hvac_reviews
    Table:    reviews   (one row per HVAC business, mirrors the CSV schema
                          but with JSONB columns for review lists)

Why a flat table that mirrors the CSV rather than a normalized schema:
  - We *also* need to export to CSV at the end of the run, so a 1:1 column
    mapping makes that trivial.
  - The website that reads this DB later can still query per-review data
    via JSONB operators (e.g. `google_maps_reviews @> '[{"rating":5}]'`).

Concurrency:
  - Up to MAX_PARALLEL_ROWS × SITES_PER_ROW (= 40) workers may write in
    parallel. ON CONFLICT (row_idx) DO UPDATE handles row-level merges.
  - For the Phase-2 retry sweep we update only the columns for the sites
    being retried, leaving previously-extracted data untouched.

Connection management:
  - psycopg2.pool.ThreadedConnectionPool with minconn=1, maxconn=16.
  - Per-call get/put pattern wrapped in `with _conn() as cur:`.
"""
import contextlib
import csv as csv_mod
import json
import logging
import os
import threading
from typing import Iterable, Optional

import psycopg2
import psycopg2.pool
from psycopg2.extras import Json, execute_values

from . import config

logger = logging.getLogger("review")

# --------------------------------------------------------------- connection

_POOL: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()


def _read_env(name: str, default: Optional[str] = None) -> str:
    """Read from os.environ first, then fall back to /var/HVAC/.env."""
    v = os.environ.get(name)
    if v is not None:
        return v
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, val = line.partition("=")
                    if k.strip() == name:
                        return val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    if default is not None:
        return default
    raise RuntimeError(f"Missing required env var: {name}")


def _ensure_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            return _POOL
        _POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=16,
            host=_read_env("POSTGRES_HOST", "127.0.0.1"),
            port=int(_read_env("POSTGRES_PORT", "5432")),
            dbname=_read_env("POSTGRES_DB"),
            user=_read_env("POSTGRES_USER"),
            password=_read_env("POSTGRES_PASSWORD"),
            connect_timeout=10,
            application_name="hvac-review-scraper",
        )
        return _POOL


@contextlib.contextmanager
def _conn(autocommit: bool = True):
    pool = _ensure_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = autocommit
        with conn.cursor() as cur:
            yield cur
        if not autocommit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------- schema

# Input columns from hvac_companies_cleaned.csv (excluding row_idx which is PK)
_INPUT_COLS = [
    "keyword", "location", "google_page", "business_name", "website_url",
    "business_description", "services_offered", "contact_phone",
    "contact_email", "address", "business_city", "business_state",
    "supply_location", "logo_url",
]

# Per-site (rating, reviews_jsonb) pairs (matches build_output_headers)
_SITE_RATING_COLS = {
    "google_maps":  ("google_maps_rating",  "google_maps_reviews"),
    "yelp":         ("yelp_rating",         "yelp_reviews"),
    "bbb":          ("bbb_rating",          None),
    "angi":         ("angi_rating",         "angi_reviews"),
    "homeadvisor":  ("homeadvisor_rating",  "homeadvisor_reviews"),
}


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS reviews (
    row_idx              INTEGER PRIMARY KEY,
    keyword              TEXT,
    location             TEXT,
    google_page          TEXT,
    business_name        TEXT,
    website_url          TEXT,
    business_description TEXT,
    services_offered     TEXT,
    contact_phone        TEXT,
    contact_email        TEXT,
    address              TEXT,
    business_city        TEXT,
    business_state       TEXT,
    supply_location      TEXT,
    logo_url             TEXT,
    google_maps_rating   REAL,
    google_maps_reviews  JSONB,
    yelp_rating          REAL,
    yelp_reviews         JSONB,
    bbb_rating           REAL,
    angi_rating          REAL,
    angi_reviews         JSONB,
    homeadvisor_rating   REAL,
    homeadvisor_reviews  JSONB,
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reviews_business_name_idx
    ON reviews(business_name);
CREATE INDEX IF NOT EXISTS reviews_state_city_idx
    ON reviews(business_state, business_city);
"""


def init_schema():
    """Create the reviews table + indexes if missing. Idempotent."""
    with _conn() as cur:
        cur.execute(SCHEMA_SQL)
    logger.info("Postgres schema ready (reviews table)")


# ---------------------------------------------------------------- writes

def _site_columns(site_id: str):
    return _SITE_RATING_COLS.get(site_id, (None, None))


def _site_payload_columns(results, site_registry, only_present: bool = False):
    """Returns (col_names, col_values) for site rating/reviews columns.

    only_present=False (default, used by upsert_full_row): emit cells for
        every site in the registry; missing sites are NULL/[].
    only_present=True  (used by upsert_site_partial): emit cells ONLY for
        sites present in `results` so we don't blow away data for sites
        that weren't part of this retry pass.
    """
    cols, vals = [], []
    for site in site_registry:
        sid = site.SITE_ID
        rating_col, reviews_col = _site_columns(sid)
        if rating_col is None:
            continue
        if only_present and sid not in results:
            continue
        r = results.get(sid) or {}
        rating = r.get("rating")
        cols.append(rating_col)
        vals.append(rating if rating is not None else None)
        if reviews_col and site.HAS_REVIEWS:
            cols.append(reviews_col)
            vals.append(Json(r.get("reviews") or []))
    return cols, vals


def upsert_full_row(row_idx: int, input_row: dict, results: dict, site_registry):
    """Write a row's input columns + site results.

    ON CONFLICT updates only the columns we wrote, leaving any prior
    site data intact (which is what we want for the Phase-2 retry sweep
    when a row gets re-processed).
    """
    cols = ["row_idx"] + _INPUT_COLS[:]
    vals = [row_idx] + [input_row.get(c, "") for c in _INPUT_COLS]

    site_cols, site_vals = _site_payload_columns(results, site_registry)
    cols.extend(site_cols)
    vals.extend(site_vals)
    cols.append("updated_at")
    vals.append(psycopg2.extensions.AsIs("now()"))

    placeholders = ",".join(["%s"] * len(vals))
    col_list = ",".join(cols)
    update_clause = ",".join(
        f"{c} = EXCLUDED.{c}"
        for c in cols
        if c not in ("row_idx", "created_at")
    )
    sql = (
        f"INSERT INTO reviews ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (row_idx) DO UPDATE SET {update_clause}"
    )
    with _conn() as cur:
        cur.execute(sql, vals)


def upsert_site_partial(row_idx: int, results: dict, site_registry):
    """Update ONLY the rating/reviews columns for the sites in `results`,
    leaving the rest of the row untouched. Used by Phase-2 retry sweep.

    If the row doesn't exist yet (very rare — only if a retry fires before
    the initial write), we INSERT a stub with row_idx + the new site cells.
    """
    site_cols, site_vals = _site_payload_columns(
        results, site_registry, only_present=True
    )
    if not site_cols:
        return
    site_cols.append("updated_at")
    site_vals.append(psycopg2.extensions.AsIs("now()"))

    cols = ["row_idx"] + site_cols
    vals = [row_idx] + site_vals
    placeholders = ",".join(["%s"] * len(vals))
    col_list = ",".join(cols)
    update_clause = ",".join(f"{c} = EXCLUDED.{c}" for c in site_cols)
    sql = (
        f"INSERT INTO reviews ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (row_idx) DO UPDATE SET {update_clause}"
    )
    with _conn() as cur:
        cur.execute(sql, vals)


def update_business_name_if_empty(row_idx: int, name: str):
    """Set business_name on a row only if it's currently NULL or empty.

    Used by the Phase-2 retry sweep to backfill names discovered during a
    retry without ever overwriting a value the user supplied or that the
    Phase-1 pass already set.
    """
    if not name:
        return
    sql = (
        "UPDATE reviews "
        "   SET business_name = %s, updated_at = now() "
        " WHERE row_idx = %s "
        "   AND (business_name IS NULL OR business_name = '')"
    )
    with _conn() as cur:
        cur.execute(sql, (name, row_idx))


# ---------------------------------------------------------------- reads

def get_completed_row_indices() -> set:
    """For resume support: which row_idxs are already in the DB."""
    with _conn() as cur:
        cur.execute("SELECT row_idx FROM reviews")
        return {r[0] for r in cur.fetchall()}


def row_count() -> int:
    with _conn() as cur:
        cur.execute("SELECT COUNT(*) FROM reviews")
        return cur.fetchone()[0]


def per_state_counts() -> list:
    with _conn() as cur:
        cur.execute(
            "SELECT business_state, COUNT(*) FROM reviews "
            "GROUP BY business_state ORDER BY 2 DESC"
        )
        return cur.fetchall()


# ---------------------------------------------------------------- migration

def import_csv(csv_path: str, site_registry) -> int:
    """One-time migration of an existing review_output.csv into the DB.

    Each CSV cell that holds a JSON list (the *_reviews columns) is parsed
    and inserted as JSONB. Empty rating cells become NULL.
    Returns number of rows imported.
    """
    init_schema()
    if not os.path.exists(csv_path):
        logger.info(f"No CSV at {csv_path} — nothing to import")
        return 0

    csv_mod.field_size_limit(10 * 1024 * 1024)
    site_review_cols = [
        f"{s.SITE_ID}_reviews"
        for s in site_registry
        if s.HAS_REVIEWS
    ]
    site_rating_cols = [f"{s.SITE_ID}_rating" for s in site_registry]

    rows = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv_mod.DictReader(f)
        for r in reader:
            row_idx_s = r.get("_row_idx", "").strip()
            if not row_idx_s.isdigit():
                continue
            row_idx = int(row_idx_s)
            tup = [row_idx] + [r.get(c, "") or None for c in _INPUT_COLS]
            for col in site_rating_cols:
                v = (r.get(col) or "").strip()
                tup.append(float(v) if v else None)
            for col in site_review_cols:
                v = (r.get(col) or "").strip()
                if not v:
                    tup.append(Json([]))
                else:
                    try:
                        tup.append(Json(json.loads(v)))
                    except Exception:
                        tup.append(Json([]))
            rows.append(tup)

    if not rows:
        logger.info("No data rows in CSV — skipping import")
        return 0

    cols = ["row_idx"] + _INPUT_COLS + site_rating_cols + site_review_cols
    update_clause = ",".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c != "row_idx"
    )
    placeholders = "(" + ",".join(["%s"] * len(cols)) + ")"
    sql = (
        f"INSERT INTO reviews ({','.join(cols)}) VALUES %s "
        f"ON CONFLICT (row_idx) DO UPDATE SET {update_clause}, updated_at=now()"
    )
    with _conn(autocommit=False) as cur:
        execute_values(cur, sql, rows, template=placeholders)
    logger.info(f"Imported {len(rows)} rows from {csv_path}")
    return len(rows)


# ---------------------------------------------------------------- export

def export_to_csv(csv_path: str, headers: list, site_registry) -> int:
    """Dump the entire reviews table to a CSV mirroring the original schema.

    Called at the end of the run (after Phase 2). Reviews JSONB columns
    are re-encoded with the same `reviews_to_cell` formatting we used
    pre-Postgres so the CSV consumers don't see any change.
    """
    from .json_utils import reviews_to_cell

    with _conn() as cur:
        cur.execute(
            "SELECT * FROM reviews ORDER BY row_idx"
        )
        col_names = [d[0] for d in cur.description]
        all_rows = cur.fetchall()

    review_cols = {f"{s.SITE_ID}_reviews" for s in site_registry if s.HAS_REVIEWS}

    written = 0
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for db_row in all_rows:
            db_dict = dict(zip(col_names, db_row))
            out = {}
            for h in headers:
                if h == "_row_idx":
                    out[h] = str(db_dict.get("row_idx", ""))
                elif h in review_cols:
                    raw = db_dict.get(h) or []
                    out[h] = reviews_to_cell(raw if isinstance(raw, list) else [])
                else:
                    v = db_dict.get(h)
                    out[h] = "" if v is None else str(v)
            writer.writerow(out)
            written += 1
    logger.info(f"Exported {written} rows to {csv_path}")
    return written


def close_pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.closeall()
            except Exception:
                pass
            _POOL = None
