"""
Progress tracking and resume capability (thread-safe, instance-count agnostic).

Uses a *set-based* model: each completed work-unit is identified by a unique
string key (e.g. "keyword||location||page").  This allows any number of
worker threads to mark work done, and the scraper can resume with a different
number of workers without losing progress.

State is persisted atomically (write-to-temp then os.replace) and debounced
to avoid thrashing the disk when many threads finish work at the same time.
"""
import json
import logging
import os
import tempfile
import threading
import time

import config

logger = logging.getLogger("scraper")


class ProgressTracker:
    """
    Thread-safe, instance-count-agnostic progress tracker.

    Tracked state:
        - completed_combos : set of "keyword||location||page" strings
        - processed_urls   : set of website URLs already fully processed
        - processed_domains: set of domains already processed (one business = one domain)
        - failed_combos    : set of combos that failed (blocked/error) — retried next run
    """

    _SAVE_INTERVAL = 5  # seconds – debounce disk writes

    def __init__(self, filepath: str = None):
        self.filepath = filepath or config.PROGRESS_FILE
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0

        self.state = {
            "completed_combos": set(),
            "processed_urls": set(),
            "failed_combos": set(),
            "processed_domains": set(),
        }
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract the root domain from a URL (strips www.)."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except Exception:
            return url

    def _load(self):
        """Load progress from disk, migrating old format if necessary."""
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load progress file, starting fresh: {e}")
            return

        # --- Migrate old sequential-index format ---
        if "current_combination_index" in saved:
            logger.info(
                "Migrating old progress format → set-based model. "
                "Processed URLs are preserved; combo tracking restarts."
            )
            self.state["processed_urls"] = set(saved.get("processed_urls", []))
            self.state["completed_combos"] = set()
            self.state["failed_combos"] = set()
            self.state["processed_domains"] = set()
            self._force_save()
            return

        # --- New format ---
        self.state["completed_combos"] = set(saved.get("completed_combos", []))
        self.state["processed_urls"] = set(saved.get("processed_urls", []))
        self.state["failed_combos"] = set(saved.get("failed_combos", []))
        self.state["processed_domains"] = set(saved.get("processed_domains", []))
        logger.info(
            f"Resumed progress: completed_combos={len(self.state['completed_combos'])}, "
            f"processed_urls={len(self.state['processed_urls'])}, "
            f"processed_domains={len(self.state['processed_domains'])}, "
            f"failed_combos={len(self.state['failed_combos'])}"
        )

    def _save_if_needed(self):
        """Debounced save – only writes to disk every _SAVE_INTERVAL seconds."""
        if not self._dirty:
            return
        now = time.time()
        if now - self._last_save < self._SAVE_INTERVAL:
            return
        self._force_save()

    def _force_save(self):
        """Atomic write: temp file → os.replace (safe on NTFS & POSIX)."""
        try:
            state_to_save = {
                "completed_combos": sorted(self.state["completed_combos"]),
                "processed_urls": sorted(self.state["processed_urls"]),
                "failed_combos": sorted(self.state["failed_combos"]),
                "processed_domains": sorted(self.state["processed_domains"]),
            }
            dir_name = os.path.dirname(self.filepath) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state_to_save, f, indent=2)
                os.replace(tmp_path, self.filepath)
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._dirty = False
            self._last_save = time.time()
        except IOError as e:
            logger.error(f"Failed to save progress: {e}")

    def flush(self):
        """Force-save current state to disk (call on shutdown)."""
        with self._lock:
            self._force_save()

    # ------------------------------------------------------------------
    # Combo key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def combo_key(keyword: str, location: str, page: int) -> str:
        """Build a deterministic key for a work-unit."""
        return f"{keyword}||{location}||{page}"

    # ------------------------------------------------------------------
    # Public API (all thread-safe)
    # ------------------------------------------------------------------

    def is_combo_completed(self, keyword: str, location: str, page: int) -> bool:
        """Check if a (keyword, location, page) combo is already done."""
        key = self.combo_key(keyword, location, page)
        with self._lock:
            return key in self.state["completed_combos"]

    def mark_combo_completed(self, keyword: str, location: str, page: int):
        """Mark a combo as fully processed and persist."""
        key = self.combo_key(keyword, location, page)
        with self._lock:
            self.state["completed_combos"].add(key)
            # If it was previously failed, remove from failed
            self.state["failed_combos"].discard(key)
            self._dirty = True
            self._save_if_needed()

    def is_url_processed(self, url: str) -> bool:
        """Check if a website URL has already been processed."""
        with self._lock:
            return url in self.state["processed_urls"]

    def claim_url(self, url: str) -> bool:
        """
        Atomically check-and-claim a URL for processing.

        Returns True if this thread successfully claimed the URL (first caller).
        Returns False if the URL was already processed or claimed by another thread.

        This eliminates the race condition where two threads both see
        is_url_processed()=False and both proceed to call AI.
        """
        with self._lock:
            if url in self.state["processed_urls"]:
                return False
            # Mark immediately so no other thread can claim it
            self.state["processed_urls"].add(url)
            self._dirty = True
            self._save_if_needed()
            return True

    def mark_url_processed(self, url: str):
        """Mark a website URL as fully processed and persist."""
        with self._lock:
            self.state["processed_urls"].add(url)
            self._dirty = True
            self._save_if_needed()

    def claim_domain(self, url: str) -> bool:
        """
        Atomically check-and-claim a domain for processing.

        Returns True if this thread is the first to claim this domain.
        Returns False if the domain was already processed by any URL.

        This prevents duplicate AI calls + CSV rows when Google returns
        different pages of the same website (e.g. example.com/services
        and example.com/about from different search pages).
        """
        domain = self._extract_domain(url)
        if not domain:
            return True  # can't extract domain, allow processing
        with self._lock:
            if domain in self.state["processed_domains"]:
                return False
            self.state["processed_domains"].add(domain)
            self._dirty = True
            self._save_if_needed()
            return True

    def is_domain_processed(self, url: str) -> bool:
        """Check if a domain has already been processed."""
        domain = self._extract_domain(url)
        with self._lock:
            return domain in self.state["processed_domains"]

    def mark_combo_failed(self, keyword: str, location: str, page: int):
        """
        Mark a combo as failed (blocked/error) so it is retried on next run.

        Failed combos are NOT counted as completed — they stay in the
        pending queue on subsequent runs.
        """
        key = self.combo_key(keyword, location, page)
        with self._lock:
            self.state["failed_combos"].add(key)
            self._dirty = True
            self._save_if_needed()

    def is_combo_failed(self, keyword: str, location: str, page: int) -> bool:
        """Check if a combo was previously failed (will be retried)."""
        key = self.combo_key(keyword, location, page)
        with self._lock:
            return key in self.state["failed_combos"]

    def clear_failed_combo(self, keyword: str, location: str, page: int):
        """Remove a combo from the failed set (e.g. when retrying it)."""
        key = self.combo_key(keyword, location, page)
        with self._lock:
            self.state["failed_combos"].discard(key)
            self._dirty = True
            self._save_if_needed()

    def reset(self):
        """Reset all progress (use with caution)."""
        with self._lock:
            self.state = {
                "completed_combos": set(),
                "processed_urls": set(),
                "failed_combos": set(),
            }
            self._force_save()
        logger.info("Progress has been reset.")

