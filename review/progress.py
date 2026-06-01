"""Crash-safe per-row per-site progress tracker.

Schema:
{
  "version": 1,
  "rows": {
    "<row_idx>": {
      "business_name": "...",
      "sites": {
        "<site_id>": {"status": "done|not_found|blocked|error|pending", "attempts": int}
      },
      "row_complete": bool          # all 17 sites are terminal
    }
  }
}

Atomic write idiom + 5-second debounce mirrors /var/HVAC/progress_tracker.py.
"""
import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger("review")

TERMINAL_STATUSES = ("done", "not_found")          # never re-tried
RETRY_STATUSES = ("blocked", "error", "pending")   # eligible for retry pass


class ReviewProgress:
    _SAVE_INTERVAL = 5  # seconds — debounce disk writes

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self.state = {"version": 1, "rows": {}}
        self._load()

    # ------------------------------------------------------------------ load/save
    def _load(self):
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load review progress, starting fresh: {e}")
            return
        if isinstance(saved, dict) and "rows" in saved:
            self.state = saved
        logger.info(f"Resumed review progress: {len(self.state['rows'])} rows tracked")

    def _force_save(self):
        try:
            dir_name = os.path.dirname(self.filepath) or "."
            fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.state, f, indent=2)
                os.replace(tmp, self.filepath)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._dirty = False
            self._last_save = time.time()
        except IOError as e:
            logger.error(f"Failed to save review progress: {e}")

    def _save_if_needed(self):
        if not self._dirty:
            return
        if time.time() - self._last_save < self._SAVE_INTERVAL:
            return
        self._force_save()

    def flush(self):
        with self._lock:
            self._force_save()

    # ------------------------------------------------------------------ row helpers
    def _row(self, row_idx, business_name=""):
        key = str(row_idx)
        r = self.state["rows"].get(key)
        if r is None:
            r = {"business_name": business_name, "sites": {}, "row_complete": False}
            self.state["rows"][key] = r
            self._dirty = True
        elif business_name and not r.get("business_name"):
            r["business_name"] = business_name
            self._dirty = True
        return r

    # ------------------------------------------------------------------ public API
    def is_row_complete(self, row_idx) -> bool:
        with self._lock:
            r = self.state["rows"].get(str(row_idx))
            return bool(r and r.get("row_complete"))

    def site_status(self, row_idx, site_id) -> str:
        with self._lock:
            r = self.state["rows"].get(str(row_idx))
            if not r:
                return "pending"
            return (r.get("sites", {}).get(site_id) or {}).get("status", "pending")

    def site_attempts(self, row_idx, site_id) -> int:
        with self._lock:
            r = self.state["rows"].get(str(row_idx))
            if not r:
                return 0
            return (r.get("sites", {}).get(site_id) or {}).get("attempts", 0)

    def mark_site(self, row_idx, site_id, status, business_name=""):
        with self._lock:
            r = self._row(row_idx, business_name)
            entry = r["sites"].get(site_id) or {"status": "pending", "attempts": 0}
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["status"] = status
            r["sites"][site_id] = entry
            self._dirty = True
            self._save_if_needed()

    def reset_site(self, row_idx, site_id):
        """Reset a site to pending so it's eligible for the retry sweep."""
        with self._lock:
            r = self.state["rows"].get(str(row_idx))
            if not r:
                return
            entry = r["sites"].get(site_id)
            if entry:
                entry["status"] = "pending"
                # Keep attempts counter so we can see total over the lifetime
                r["row_complete"] = False
                self._dirty = True
                self._save_if_needed()

    def mark_row_complete(self, row_idx, business_name=""):
        with self._lock:
            r = self._row(row_idx, business_name)
            r["row_complete"] = True
            self._dirty = True
            self._save_if_needed()

    def collect_retry_targets(self, all_site_ids):
        """Return [(row_idx_int, [site_id, ...]), ...] for rows with any
        non-terminal-success site (status in blocked/error)."""
        targets = []
        with self._lock:
            for key, r in self.state["rows"].items():
                bad = []
                sites = r.get("sites") or {}
                for sid in all_site_ids:
                    st = (sites.get(sid) or {}).get("status", "pending")
                    if st in ("blocked", "error"):
                        bad.append(sid)
                if bad:
                    try:
                        targets.append((int(key), bad))
                    except ValueError:
                        continue
        targets.sort(key=lambda t: t[0])
        return targets

    def per_site_stats(self, all_site_ids):
        """Return {site_id: {done:N, not_found:N, blocked:N, error:N, pending:N}}."""
        stats = {sid: {"done": 0, "not_found": 0, "blocked": 0, "error": 0, "pending": 0}
                 for sid in all_site_ids}
        with self._lock:
            for r in self.state["rows"].values():
                for sid in all_site_ids:
                    st = (r.get("sites", {}).get(sid) or {}).get("status", "pending")
                    if st not in stats[sid]:
                        st = "pending"
                    stats[sid][st] += 1
        return stats
