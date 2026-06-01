"""Review-scraper orchestrator.

Phase 1: process each row sequentially, with the 17 site scrapers running
         in parallel within the row.
Phase 2: end-of-run retry sweep — re-runs every site marked blocked/error
         (up to RETRY_MAX_PASSES times) so missed data gets filled before exit.

Usage:
    python -m review.runner                          # process all rows
    python -m review.runner --rows 20                # only first 20 rows (test)
    python -m review.runner --output some.csv        # custom output path
    python -m review.runner --progress some.json     # custom progress path
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import browser
from . import cleanup_daemon
from . import csv_io
from . import db as review_db
from . import proxies as proxies_mod
from .progress import ReviewProgress
from .sites import SITE_REGISTRY, ALL_SITE_IDS, get_scraper
from .base_scraper import (
    STATUS_DONE, STATUS_NOT_FOUND, STATUS_BLOCKED, STATUS_ERROR, empty_result,
)

logger = logging.getLogger("review")
_shutdown = False


def _install_signal_handlers(tracker):
    def _handler(sig, frame):
        global _shutdown
        logger.warning(f"Received signal {sig} — flushing progress and exiting")
        _shutdown = True
        try:
            tracker.flush()
        except Exception:
            pass
        cleanup_daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _run_one_site(scraper, row, proxies, tracker, row_idx):
    """Execute one site for one row. Updates progress."""
    sid = scraper.SITE_ID
    biz = row.get("business_name", "")
    try:
        result = browser.run_with_retries(scraper, row, proxies,
                                          max_attempts=scraper.MAX_RETRIES)
    except Exception as e:
        logger.error(f"[r{row_idx}/{sid}] orchestrator threw: {type(e).__name__}: {e}")
        result = empty_result(STATUS_ERROR)

    status = result.get("status", STATUS_ERROR)
    tracker.mark_site(row_idx, sid, status, business_name=biz)
    return result


def process_row(row_idx, row, proxies, tracker,
                output_csv, headers, only_sites=None, force_csv_update=False):
    """Process a single business row.

    only_sites=None  → run every non-terminal site, then append a fresh row
    only_sites=[id]  → run just those sites (used by retry sweep);
                       update existing row in-place if force_csv_update is True
    """
    if _shutdown:
        return "SHUTDOWN"

    biz = row.get("business_name", "")

    if only_sites is None:
        if tracker.is_row_complete(row_idx):
            return "SKIP"
        pending = [s for s in SITE_REGISTRY
                   if tracker.site_status(row_idx, s.SITE_ID) not in ("done", "not_found")]
        if not pending:
            tracker.mark_row_complete(row_idx, business_name=biz)
            return "DONE"
    else:
        pending = [get_scraper(sid) for sid in only_sites if get_scraper(sid)]
        if not pending:
            return "DONE"

    logger.info(f"[r{row_idx}] '{biz}' — {len(pending)} sites: "
                f"{[s.SITE_ID for s in pending]}")

    results = {}
    with ThreadPoolExecutor(max_workers=config.SITES_PER_ROW,
                            thread_name_prefix=f"r{row_idx}") as ex:
        futures = {ex.submit(_run_one_site, s, row, proxies, tracker, row_idx): s
                   for s in pending}
        try:
            for fut in as_completed(futures, timeout=config.ROW_HARD_TIMEOUT):
                site = futures[fut]
                try:
                    results[site.SITE_ID] = fut.result()
                except Exception as e:
                    logger.error(f"[r{row_idx}/{site.SITE_ID}] future raised: {e}")
                    results[site.SITE_ID] = empty_result(STATUS_ERROR)
                    tracker.mark_site(row_idx, site.SITE_ID, STATUS_ERROR,
                                      business_name=biz)
        except TimeoutError:
            logger.warning(f"[r{row_idx}] row hard-timeout {config.ROW_HARD_TIMEOUT}s — "
                           "harvesting any futures that already finished")
            # CRITICAL: don't lose results that completed AROUND the timeout.
            # Sweep remaining futures and collect any that are done.
            for fut, site in futures.items():
                if site.SITE_ID in results:
                    continue
                if fut.done():
                    try:
                        results[site.SITE_ID] = fut.result(timeout=1)
                    except Exception:
                        pass

    # If the input had no business_name, backfill it from whatever the
    # scrapers discovered. Priority: google_maps → bbb → angi → homeadvisor
    # → yelp (most → least authoritative for a US-local-business listing).
    if not (row.get("business_name") or "").strip():
        for sid in ("google_maps", "bbb", "angi", "homeadvisor", "yelp"):
            r = results.get(sid) or {}
            disc = (r.get("business_name") or "").strip() if isinstance(r, dict) else ""
            if disc:
                row["business_name"] = disc
                logger.info(
                    f"[r{row_idx}] backfilled business_name from {sid}: {disc!r}"
                )
                break

    # Initial pass → write full row (input cols + all site results).
    # Retry pass → partial update of just the retried sites' columns.
    try:
        if only_sites is None and not force_csv_update:
            review_db.upsert_full_row(row_idx, row, results, SITE_REGISTRY)
        else:
            review_db.upsert_site_partial(row_idx, results, SITE_REGISTRY)
            # Phase 2 retry sweep: backfill empty business_name in DB if a
            # retried site discovered one.
            disc_now = (row.get("business_name") or "").strip()
            if disc_now:
                review_db.update_business_name_if_empty(row_idx, disc_now)
    except Exception as e:
        logger.error(f"[r{row_idx}] DB write failed: {type(e).__name__}: {e}")

    tracker.mark_row_complete(row_idx, business_name=biz)
    return "DONE"


def _merge_with_completed(row_idx, results, tracker):
    """Build a results dict that includes previously-completed sites' empty
    placeholder (rating=None, reviews=[]) for the columns. Sites not in
    `results` AND not previously done stay empty in the CSV."""
    merged = dict(results)
    for s in SITE_REGISTRY:
        if s.SITE_ID in merged:
            continue
        # Previously completed but not re-run this pass — show empty cells
        # (the historical rating/reviews aren't stored in progress.json by
        # design; only the *current* run's results are in results).
        merged[s.SITE_ID] = empty_result(tracker.site_status(row_idx, s.SITE_ID))
    return merged


def retry_blocked_pass(rows, proxies, tracker, output_csv, headers,
                       max_passes=None):
    max_passes = max_passes or config.RETRY_MAX_PASSES
    for pass_num in range(1, max_passes + 1):
        if _shutdown:
            return
        targets = tracker.collect_retry_targets(ALL_SITE_IDS)
        if not targets:
            logger.info("[Retry] No blocked/error entries remain — done")
            return
        total = sum(len(s) for _, s in targets)
        logger.info(
            f"[Retry pass {pass_num}/{max_passes}] {total} entries across {len(targets)} rows"
        )
        for row_idx, site_ids in targets:
            if _shutdown:
                return
            if row_idx >= len(rows):
                continue
            row = rows[row_idx]
            for sid in site_ids:
                tracker.reset_site(row_idx, sid)
            process_row(row_idx, row, proxies, tracker,
                        output_csv, headers,
                        only_sites=site_ids, force_csv_update=True)
        if pass_num < max_passes:
            logger.info(f"[Retry pass {pass_num}] sleep {config.RETRY_INTERPASS_SLEEP}s")
            time.sleep(config.RETRY_INTERPASS_SLEEP)


def collect_per_site_stats(tracker):
    return tracker.per_site_stats(ALL_SITE_IDS)


def print_per_site_table(stats):
    cols = ("done", "not_found", "blocked", "error", "pending")
    width = max(len(s) for s in stats.keys())
    header = f"{'site':<{width}}  " + "  ".join(f"{c:>9}" for c in cols)
    print(header)
    print("-" * len(header))
    for sid, st in stats.items():
        line = f"{sid:<{width}}  " + "  ".join(f"{st.get(c,0):>9}" for c in cols)
        print(line)


def process_rows(rows, proxies, tracker, output_csv, headers,
                 retry_passes=None, start_index=0, end_index=None):
    """Parallel row loop + final retry sweep.

    Up to config.MAX_PARALLEL_ROWS rows are processed concurrently. Each row
    fans out to SITES_PER_ROW sites internally, so total concurrent Chrome
    subprocesses = MAX_PARALLEL_ROWS × SITES_PER_ROW.

    Thread-safety:
      - csv_io._csv_lock serializes appends to the output CSV
      - tracker._lock serializes progress.json writes
      - each row's site-level work runs in its own ThreadPoolExecutor
    """
    end = end_index if end_index is not None else len(rows)
    total = end - start_index
    n_workers = max(1, getattr(config, "MAX_PARALLEL_ROWS", 1))
    logger.info(
        f"Processing rows [{start_index}:{end}] (total={total}) "
        f"with row_concurrency={n_workers}, sites_per_row={config.SITES_PER_ROW}"
    )

    completed = 0
    completed_lock = threading.Lock()

    def _do_row(idx):
        nonlocal completed
        if _shutdown:
            return "SHUTDOWN"
        try:
            status = process_row(
                idx, rows[idx], proxies, tracker, output_csv, headers
            )
        except Exception as e:
            logger.error(f"[r{idx}] unhandled: {type(e).__name__}: {e}")
            status = "ERROR"
        with completed_lock:
            completed += 1
            done_count = completed
        logger.info(f"[r{idx}] {status} ({done_count}/{total})")
        return status

    with ThreadPoolExecutor(max_workers=n_workers,
                            thread_name_prefix="row") as ex:
        futures = [ex.submit(_do_row, i) for i in range(start_index, end)]
        try:
            for fut in as_completed(futures):
                if _shutdown:
                    break
                fut.result()  # surfaces unexpected exceptions to the log
        except KeyboardInterrupt:
            logger.warning("KeyboardInterrupt — cancelling pending row futures")
            for f in futures:
                f.cancel()
            raise

    if not _shutdown:
        logger.info("=== Phase 1 complete — starting Phase 2 retry sweep ===")
        retry_blocked_pass(rows, proxies, tracker, output_csv, headers,
                           max_passes=retry_passes)


def main(argv=None):
    parser = argparse.ArgumentParser(description="HVAC review scraper")
    parser.add_argument("--rows", type=int, default=None,
                        help="Process only the first N rows (testing).")
    parser.add_argument("--start", type=int, default=0,
                        help="Start at this row index (default 0).")
    parser.add_argument("--input", default=config.INPUT_CSV)
    parser.add_argument("--output", default=config.OUTPUT_CSV)
    parser.add_argument("--progress", default=config.PROGRESS_FILE)
    parser.add_argument("--log-file", default=config.LOG_FILE)
    parser.add_argument("--retry-passes", type=int, default=config.RETRY_MAX_PASSES)
    args = parser.parse_args(argv)

    config.setup_logging(log_file=args.log_file)

    cleanup_daemon.reap_now()
    cleanup_daemon.start()

    proxies = proxies_mod.load_proxies()
    logger.info(f"Loaded {len(proxies)} proxies")

    headers, rows = csv_io.read_input_csv(args.input)
    out_headers = config.build_output_headers(headers, SITE_REGISTRY)

    # Postgres is the source of truth — initialize schema. The output CSV is
    # exported AT THE END (after Phase 2) so we don't write CSV during the run.
    review_db.init_schema()

    tracker = ReviewProgress(args.progress)
    _install_signal_handlers(tracker)

    end_index = (args.start + args.rows) if args.rows else len(rows)
    end_index = min(end_index, len(rows))

    try:
        process_rows(rows, proxies, tracker, args.output, out_headers,
                     retry_passes=args.retry_passes,
                     start_index=args.start, end_index=end_index)
    finally:
        tracker.flush()
        cleanup_daemon.stop()

    stats = collect_per_site_stats(tracker)
    print()
    print_per_site_table(stats)
    print()

    # End-of-run export: dump DB → CSV so users still get a flat artifact.
    if not _shutdown:
        try:
            n = review_db.export_to_csv(args.output, out_headers, SITE_REGISTRY)
            logger.info(f"Final CSV export: {n} rows → {args.output}")
        except Exception as e:
            logger.error(f"CSV export failed: {type(e).__name__}: {e}")

    review_db.close_pool()
    logger.info("Run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
