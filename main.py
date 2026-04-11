"""
Main orchestrator for the Google Search Web Scraper.

Reads input CSV, generates keyword-location combinations, scrapes Google,
visits websites, extracts data via AI, and writes results to CSV.

Supports multi-threaded execution (--workers N) with crash-safe resume.
Progress is tracked per work-unit (keyword + location + page), so the
number of workers can change between runs without losing progress.
"""
import argparse
import csv
import multiprocessing
import os
import random
import signal
import subprocess
import sys
import time
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product

import config
from config import setup_logging
from url_generator import generate_all_search_urls
from google_scraper import load_proxies, scrape_google_page
from website_extractor import extract_website_content
from ai_extractor import extract_business_data, save_content_cache
from progress_tracker import ProgressTracker

# Thread lock for CSV writes
_csv_lock = threading.Lock()

# Event set on Ctrl+C — workers check before starting new work
_shutdown_event = threading.Event()


def read_input_csv(filepath: str) -> tuple:
    """
    Read input CSV with 'keywords' and 'locations' columns.

    Returns:
        Tuple of (keywords_list, locations_list).
    """
    keywords = []
    locations = []
    with open(filepath, "r", encoding="latin-1") as f:
        reader = csv.DictReader(f)
        for row in reader:
            kw = row.get("keywords", "").strip()
            loc = row.get("locations", "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
            if loc and loc not in locations:
                locations.append(loc)
    return keywords, locations


def generate_combinations(keywords: list, locations: list) -> list:
    """
    Generate cartesian product of keywords and locations.

    Order: keyword1+location1, keyword1+location2, ..., keyword2+location1, ...

    Returns:
        List of (keyword, location) tuples.
    """
    return list(product(keywords, locations))


def initialize_output_csv(filepath: str):
    """Create output CSV with headers if it doesn't exist."""
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=config.OUTPUT_HEADERS)
            writer.writeheader()


def append_to_output_csv(filepath: str, data: dict, keyword: str, location: str, page: int):
    """
    Append a single record to the output CSV immediately (thread-safe).

    This ensures no data loss if the scraper crashes.
    """
    row = {
        "keyword": keyword,
        "location": location,
        "google_page": page + 1,  # 1-based page number for display
        "business_name": data.get("business_name", ""),
        "website_url": data.get("website_url", ""),
        "business_description": data.get("business_description", ""),
        "services_offered": data.get("services_offered", ""),
        "contact_phone": data.get("contact_phone", ""),
        "contact_email": data.get("contact_email", ""),
        "address": data.get("address", ""),
        "business_city": data.get("business_city", ""),
        "business_state": data.get("business_state", ""),
        "logo_url": data.get("logo_url", ""),
    }
    with _csv_lock:
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=config.OUTPUT_HEADERS)
            writer.writerow(row)


def process_website(url, keyword, location, page, proxies, tracker, logger):
    """Process a single website: extract content, run AI, save result."""
    # Domain-level dedup — one domain = one business.
    # If Google returned example.com/services on page 1 and
    # example.com/about on page 3, only the first one gets processed.
    # This saves AI tokens and prevents duplicate CSV rows.
    if not tracker.claim_domain(url):
        logger.info(f"Skipping already processed domain: {url}")
        return

    # URL-level claim (still needed for exact-URL dedup within same domain claim)
    if not tracker.claim_url(url):
        logger.info(f"Skipping already claimed/processed URL: {url}")
        return

    if _shutdown_event.is_set():
        return

    logger.info(f"Processing website: {url}")

    # Step 1: Extract website content (homepage, contact page, logo)
    content = extract_website_content(url)

    # Step 2: Send to AI for extraction
    business_data = extract_business_data(
        homepage_html=content["homepage_html"],
        contact_html=content["contact_html"],
        logo_url=content["logo_url"],
        website_url=url,
    )

    # Step 3: Immediately append to output CSV (thread-safe)
    append_to_output_csv(config.OUTPUT_CSV, business_data, keyword, location, page)

    # Step 4: Mark URL as processed
    tracker.mark_url_processed(url)

    logger.info(f"Completed: {business_data.get('business_name', 'Unknown')} - {url}")

    # Rate limiting between website visits
    time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))





def worker_process_combo(keyword, location, page_idx, search_url, proxies, tracker, logger):
    """
    Worker function: scrape one Google page and process all URLs found.

    This is the unit of work submitted to the thread pool.
    Each call handles one (keyword, location, page) combination.
    """
    if _shutdown_event.is_set():
        return f"SHUTDOWN: {keyword} | {location} | page {page_idx + 1}"

    # Check if this combo was already completed in a previous run
    if tracker.is_combo_completed(keyword, location, page_idx):
        return f"SKIP (done): {keyword} | {location} | page {page_idx + 1}"

    label = f"'{keyword}' + '{location}' page {page_idx + 1}"
    logger.info(f"[Worker] Starting: {label}")

    # Clear previous failure flag if retrying
    tracker.clear_failed_combo(keyword, location, page_idx)

    # Scrape Google results page
    try:
        scrape_result = scrape_google_page(search_url, proxies)
    except Exception as e:
        logger.error(
            f"CRASH in scrape_google_page for {label}: "
            f"{type(e).__name__}: {e}"
        )
        logger.error(traceback.format_exc())
        scrape_result = {"urls": [], "status": "error"}
        time.sleep(5)

    status = scrape_result.get("status", "empty")
    website_urls = scrape_result.get("urls", [])

    if not website_urls:
        if status == "blocked":
            # Google blocked us on all retries — mark as FAILED, not done.
            # This combo will be retried on the next run.
            logger.warning(
                f"[Worker] BLOCKED for {label} — marked as failed (will retry next run)"
            )
            tracker.mark_combo_failed(keyword, location, page_idx)
            return f"BLOCKED: {label}"
        else:
            # Genuinely no results (empty page or error) — mark done
            logger.info(f"[Worker] No results for {label}, marking done.")
            tracker.mark_combo_completed(keyword, location, page_idx)
            return f"NO RESULTS: {label}"

    # Process each website found on this Google page
    for url in website_urls:
        if _shutdown_event.is_set():
            # Don't mark combo as completed — we'll resume these URLs
            return f"SHUTDOWN mid-combo: {label}"
        try:
            process_website(
                url, keyword, location, page_idx, proxies, tracker, logger
            )
        except Exception as e:
            logger.error(
                f"CRASH in process_website for {url}: "
                f"{type(e).__name__}: {e}"
            )
            logger.error(traceback.format_exc())
            tracker.mark_url_processed(url)

    # All URLs on this page done — mark combo complete
    tracker.mark_combo_completed(keyword, location, page_idx)
    logger.info(f"[Worker] Completed: {label}")
    return f"DONE: {label}"


def main():
    """Main entry point for the scraper."""
    # Parse CLI arguments
    parser = argparse.ArgumentParser(description="Google Search Web Scraper")
    parser.add_argument(
        "--workers", type=int, default=config.NUM_WORKERS,
        help=f"Number of concurrent worker threads (default: {config.NUM_WORKERS})",
    )
    args = parser.parse_args()
    num_workers = max(1, args.workers)

    logger = setup_logging()
    logger.info("=" * 60)
    logger.info(f"Google Search Web Scraper - Starting ({num_workers} workers)")
    logger.info("=" * 60)

    # Kill only orphaned Chrome/Chromedriver processes left by a previous HVAC run.
    # Orphaned = parent PID is 1 (systemd/init), meaning their parent already died.
    # This avoids killing Chrome processes belonging to other apps on this server.
    try:
        result = subprocess.run(
            ["bash", "-c",
             "ps -eo ppid,pid,comm | awk '$1==1 && ($3~/chrome/ || $3~/chromedriver/) {print $2}'"],
            capture_output=True, text=True,
        )
        killed_count = 0
        for pid_str in result.stdout.strip().split("\n"):
            pid_str = pid_str.strip()
            if pid_str:
                subprocess.run(["kill", "-9", pid_str], capture_output=True)
                killed_count += 1
        if killed_count:
            logger.info(f"Killed {killed_count} orphaned Chrome processes from previous run")
    except Exception as e:
        logger.warning(f"Could not clean orphaned Chrome processes: {e}")

    # Load proxies
    proxies = load_proxies()
    if not proxies:
        logger.warning("No proxies loaded! Scraping without proxies.")
    else:
        logger.info(f"Loaded {len(proxies)} proxies")

    # Read input CSV
    if not os.path.exists(config.INPUT_CSV):
        logger.error(f"Input file not found: {config.INPUT_CSV}")
        print(f"ERROR: Please create '{config.INPUT_CSV}' with 'keywords' and 'locations' columns.")
        return

    keywords, locations = read_input_csv(config.INPUT_CSV)
    logger.info(f"Loaded {len(keywords)} keywords and {len(locations)} locations")

    # Generate all combinations
    combinations = generate_combinations(keywords, locations)
    total_combos = len(combinations)
    logger.info(f"Total keyword-location combinations: {total_combos}")

    if total_combos == 0:
        logger.error("No valid keyword-location combinations found.")
        return

    # Initialize output CSV and progress tracker
    initialize_output_csv(config.OUTPUT_CSV)
    tracker = ProgressTracker()

    max_pages = config.MAX_GOOGLE_PAGES
    total_work = total_combos * max_pages
    logger.info(f"Total work units: {total_work} ({total_combos} combos × {max_pages} pages)")

    # Shuffle combinations once (1.3M items, ~100MB — fast and safe)
    # Workers will spread across different combos immediately.
    random.shuffle(combinations)
    logger.info("Combinations shuffled. Starting workers...")

    # Install Ctrl+C handler
    original_sigint = signal.getsignal(signal.SIGINT)

    def _signal_handler(signum, frame):
        if _shutdown_event.is_set():
            logger.warning("Force shutdown requested!")
            sys.exit(1)
        logger.warning("Shutdown requested (Ctrl+C). Finishing in-flight work...")
        print("\nShutting down gracefully... (press Ctrl+C again to force)")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)

    completed_count = 0
    already_done = 0
    failed_count = 0

    def _work_generator():
        """Stream work units on demand — zero extra memory."""
        for page_idx in range(max_pages):
            for keyword, location in combinations:
                if _shutdown_event.is_set():
                    return
                if tracker.is_combo_completed(keyword, location, page_idx):
                    yield None
                    continue
                search_url = generate_all_search_urls(keyword, location, max_pages=max_pages)[page_idx]
                yield (keyword, location, page_idx, search_url)

    try:
        with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="scraper") as executor:
            future_to_wu = {}

            for wu in _work_generator():
                if _shutdown_event.is_set():
                    break
                if wu is None:
                    already_done += 1
                    continue
                keyword, location, page_idx, search_url = wu
                future = executor.submit(
                    worker_process_combo,
                    keyword, location, page_idx, search_url,
                    proxies, tracker, logger,
                )
                future_to_wu[future] = (keyword, location, page_idx)

                # Drain completed futures to keep dict bounded
                if len(future_to_wu) >= num_workers * 4:
                    done_futures = [f for f in future_to_wu if f.done()]
                    for future in done_futures:
                        wu_done = future_to_wu.pop(future)
                        try:
                            result = future.result()
                            if result and not result.startswith("BLOCKED"):
                                completed_count += 1
                            else:
                                failed_count += 1
                            logger.info(
                                f"[Progress] {completed_count + already_done}/{total_work} done, "
                                f"{failed_count} blocked — {result}"
                            )
                        except Exception as e:
                            failed_count += 1
                            logger.error(
                                f"Worker exception {wu_done[0]}|{wu_done[1]}|p{wu_done[2]}: "
                                f"{type(e).__name__}: {e}"
                            )

            for future in as_completed(future_to_wu):
                wu_done = future_to_wu[future]
                try:
                    result = future.result()
                    if result and not result.startswith("BLOCKED"):
                        completed_count += 1
                    else:
                        failed_count += 1
                    logger.info(
                        f"[Progress] {completed_count + already_done}/{total_work} done, "
                        f"{failed_count} blocked — {result}"
                    )
                except Exception as e:
                    failed_count += 1
                    logger.error(
                        f"Worker exception {wu_done[0]}|{wu_done[1]}|p{wu_done[2]}: "
                        f"{type(e).__name__}: {e}"
                    )

                if _shutdown_event.is_set():
                    logger.info("Cancelling remaining futures...")
                    for f in future_to_wu:
                        f.cancel()
                    break
    finally:
        # Ensure progress and AI content cache are flushed to disk
        tracker.flush()
        save_content_cache()
        signal.signal(signal.SIGINT, original_sigint)

    logger.info("=" * 60)
    if _shutdown_event.is_set():
        logger.info(
            f"Scraper stopped by user. Progress saved. "
            f"Completed {completed_count}/{total_work} combos."
        )
    else:
        logger.info("Scraping complete! Results saved to: " + config.OUTPUT_CSV)
    logger.info("=" * 60)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        print("\nScraper stopped by user. Progress saved.")
    except Exception as e:
        # Last-resort crash logging
        logging.basicConfig(level=logging.ERROR)
        logging.error(f"FATAL CRASH: {type(e).__name__}: {e}")
        logging.error(traceback.format_exc())
        # Also write to a crash file for post-mortem
        with open("crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"FATAL CRASH at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(traceback.format_exc())
            f.write(f"{'='*60}\n")
        print(f"FATAL CRASH: {e} — see crash.log for details")

