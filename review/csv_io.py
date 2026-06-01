"""Input CSV reader, output writer (atomic per-row append + in-place update).

Output CSV columns = ['_row_idx'] + INPUT_HEADERS + per-site columns.
Each row also gets a stable `_row_idx` (the original input CSV row index)
which is used by the resume/retry logic.
"""
import csv
import logging
import os
import tempfile
import threading

from . import config
from .json_utils import reviews_to_cell

logger = logging.getLogger("review")

_csv_lock = threading.Lock()
csv.field_size_limit(10 * 1024 * 1024)  # 10MB cells (reviews JSON can be large)


def read_input_csv(filepath: str = None):
    """Returns (headers, rows). rows is a list of dicts."""
    filepath = filepath or config.INPUT_CSV
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    logger.info(f"Loaded {len(rows)} rows from {filepath} ({len(headers)} columns)")
    return headers, rows


def initialize_output_csv(filepath: str, headers: list):
    """Create the output CSV with header row if it doesn't exist."""
    if os.path.exists(filepath):
        return
    with _csv_lock:
        if os.path.exists(filepath):
            return
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()


def already_written_row_indices(filepath: str) -> set:
    """Read the output CSV (if any) and collect all _row_idx values present."""
    if not os.path.exists(filepath):
        return set()
    seen = set()
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            v = r.get("_row_idx", "")
            if v.isdigit():
                seen.add(int(v))
    return seen


def append_review_row(filepath, headers, row_idx, input_row, results, site_registry):
    """Append a single completed row to the output CSV (thread-safe).

    `results` = {site_id: {status, rating, reviews}}
    """
    out = {"_row_idx": str(row_idx)}
    for h in headers:
        if h == "_row_idx":
            continue
        if h.endswith("_rating") or h.endswith("_reviews"):
            continue
        out[h] = input_row.get(h, "")

    for site in site_registry:
        sid = site.SITE_ID
        r = results.get(sid) or {}
        rating = r.get("rating")
        out[f"{sid}_rating"] = "" if rating is None else str(rating)
        if site.HAS_REVIEWS:
            out[f"{sid}_reviews"] = reviews_to_cell(r.get("reviews") or [])

    with _csv_lock:
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writerow(out)


def update_review_row_inplace(filepath, headers, row_idx, results_to_merge, site_registry):
    """Rewrite the row with `_row_idx == row_idx`, merging in results for the
    listed sites WITHOUT touching previously-filled site columns.

    Used by the Phase-2 retry sweep so a successful retry doesn't overwrite
    sites that were already successfully scraped on the first pass.
    """
    if not os.path.exists(filepath):
        return False
    target = str(row_idx)
    site_id_set = {s.SITE_ID for s in site_registry}

    with _csv_lock:
        dir_name = os.path.dirname(filepath) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        replaced = False
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as out_f, \
                 open(filepath, "r", encoding="utf-8", newline="") as in_f:
                reader = csv.DictReader(in_f)
                writer = csv.DictWriter(out_f, fieldnames=headers)
                writer.writeheader()
                for r in reader:
                    if r.get("_row_idx") == target:
                        for sid, payload in results_to_merge.items():
                            if sid not in site_id_set:
                                continue
                            site = next((s for s in site_registry if s.SITE_ID == sid), None)
                            if site is None:
                                continue
                            rating = payload.get("rating")
                            r[f"{sid}_rating"] = "" if rating is None else str(rating)
                            if site.HAS_REVIEWS:
                                r[f"{sid}_reviews"] = reviews_to_cell(payload.get("reviews") or [])
                        replaced = True
                    writer.writerow(r)
            os.replace(tmp, filepath)
            return replaced
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
