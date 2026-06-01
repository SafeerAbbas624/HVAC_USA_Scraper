"""JSON serialization helpers for the reviews CSV cell."""
import json


def reviews_to_cell(reviews):
    """Serialize a list of review dicts to a JSON string suitable for a CSV cell.

    csv.DictWriter automatically escapes embedded quotes; we only need to
    produce a clean JSON string. ensure_ascii=False keeps unicode readable.
    """
    if reviews is None:
        return ""
    if not isinstance(reviews, list):
        return ""
    cleaned = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        cleaned.append({
            "author": _truncate(_clean_text(r.get("author", "")), 200),
            "rating": r.get("rating"),
            "date": _truncate(_clean_text(r.get("date", "")), 60),
            "text": _truncate(_clean_text(r.get("text", "")), 4000),
        })
    return json.dumps(cleaned, ensure_ascii=False)


def cell_to_reviews(cell):
    """Inverse of reviews_to_cell — used by csv_io row update."""
    if not cell:
        return []
    try:
        v = json.loads(cell)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _clean_text(s):
    if not s:
        return ""
    s = str(s).replace("\x00", "").replace("\r", " ")
    return " ".join(s.split())


def _truncate(s, max_len):
    if not s:
        return s
    return s if len(s) <= max_len else s[:max_len] + "…"
