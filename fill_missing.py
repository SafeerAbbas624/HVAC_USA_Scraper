"""
fill_missing.py — SeleniumBase-based gap filler for hvac_companies_cleaned.csv.

Reads the cleaned CSV, finds rows where any of the 10 target fields are empty,
visits each domain with SeleniumBase (UC mode + Xvfb + proxies) to bypass
Cloudflare/captcha, sends the HTML to the AI extractor, and writes ONLY the
previously-empty fields back into the same row in-place.

Usage:
    python fill_missing.py [--workers N] [--test] [--csv PATH]

Options:
    --workers N     Number of concurrent worker threads (default: config.NUM_WORKERS)
    --test          Process only the first 2 rows that need filling (1 worker)
    --csv PATH      Path to the CSV file (default: hvac_companies_cleaned.csv)
"""
import argparse
import csv
import gc
import json
import logging
import multiprocessing
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from seleniumbase import SB

import config
from google_scraper import (
    load_proxies,
    get_random_proxy,
    _find_chrome_binary,
    _USER_AGENTS,
    _WINDOW_SIZES,
    _safe_kill_process,
)
from socks_bridge import Socks5Bridge
from ai_extractor import extract_business_data, save_content_cache
from website_extractor import extract_logo_url

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CSV = os.path.join(_BASE_DIR, "hvac_companies_cleaned.csv")
_PROGRESS_FILE = os.path.join(_BASE_DIR, "fill_missing_progress.json")
_LOG_FILE = os.path.join(_BASE_DIR, "fill_missing.log")

# Fields to fill when empty (website_url is the navigation key, not filled)
_TARGET_FIELDS = [
    "business_name",
    "business_description",
    "services_offered",
    "contact_phone",
    "contact_email",
    "address",
    "business_city",
    "business_state",
    "supply_location",
    "logo_url",
]

# Hard timeout for each website scrape attempt (seconds)
# 180s matches google_scraper.py — SB UC mode startup alone takes 30-40s on this server
_SCRAPE_TIMEOUT = 180
_MAX_RETRIES = 5
_BACKOFF_BASE = 3
_BACKOFF_MAX = 30

# Hourly memory cleanup interval (seconds)
_MEM_CLEANUP_INTERVAL = 300

# ---------------------------------------------------------------------------
# Shared state (protected by locks)
# ---------------------------------------------------------------------------
_csv_lock = threading.Lock()        # protects in-memory rows list + CSV writes
_progress_lock = threading.Lock()   # protects processed_urls set + JSON writes
_shutdown_event = threading.Event()


# ---------------------------------------------------------------------------
# Logging — separate file so output doesn't mix with main scraper
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    """Configure a dedicated logger for fill_missing."""
    logger = logging.getLogger("fill_missing")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Hourly memory cleanup daemon (identical to main.py)
# Prevents OOM on long runs by:
#   1. Python GC — frees cyclic objects
#   2. drop_caches — frees Linux page/dentry/inode cache
#   3. SIGKILL orphaned Chrome/Chromedriver processes (ppid=1 only)
# ---------------------------------------------------------------------------
def _memory_cleanup_loop(logger: logging.Logger):
    """Background daemon: GC + drop page cache + reap orphaned Chrome."""
    while not _shutdown_event.wait(timeout=_MEM_CLEANUP_INTERVAL):
        try:
            # 1. Python garbage collection
            gc_collected = gc.collect()

            # 2. Drop Linux page cache (safe for running processes)
            drop_ok = False
            try:
                subprocess.run(["/bin/sync"], capture_output=True)
                with open("/proc/sys/vm/drop_caches", "w") as _f:
                    _f.write("1\n")
                drop_ok = True
            except Exception as _e:
                logger.warning(f"[MemClean] drop_caches failed: {_e}")

            # 3. Reap orphaned Chrome/Chromedriver zombies (ppid=1 only).
            #    A PPID of 1 means the original parent already died and the
            #    process was reparented to init/systemd — i.e. truly orphaned.
            #    PermissionError on os.kill(ppid, 0) does NOT imply the parent
            #    is dead, so the previous "parent dead OR unsignalable" check
            #    could SIGKILL Chrome processes whose parent is alive.
            killed_orphans = 0
            try:
                ps_result = subprocess.run(
                    ["/bin/ps", "-eo", "ppid,pid,comm"],
                    capture_output=True, text=True,
                )
                import signal as _signal
                for line in ps_result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] == "1":
                        comm = parts[2]
                        if "chrome" in comm or "chromedriver" in comm:
                            try:
                                os.kill(int(parts[1]), _signal.SIGKILL)
                                killed_orphans += 1
                            except (ProcessLookupError, PermissionError):
                                pass
            except Exception as _e:
                logger.warning(f"[MemClean] orphan reap failed: {_e}")

            logger.info(
                f"[MemClean] GC freed {gc_collected} objects | "
                f"drop_caches={'OK' if drop_ok else 'FAILED'} | "
                f"orphan chrome killed={killed_orphans}"
            )
        except Exception as e:
            logger.warning(f"[MemClean] Error during cleanup: {e}")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def load_csv(filepath: str):
    """
    Load CSV into (rows, fieldnames).

    Returns:
        (list[dict], list[str]) — rows and original field order.
    """
    rows = []
    fieldnames = []
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            rows.append(dict(row))
    return rows, fieldnames


def save_csv(rows, fieldnames, filepath: str):
    """Atomically write all rows to CSV (temp file → os.replace)."""
    dir_name = os.path.dirname(filepath) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, filepath)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def row_needs_processing(row: dict) -> bool:
    """
    Return True if the row has a website_url and at least one target field is empty.
    """
    if not row.get("website_url", "").strip():
        return False
    return any(not row.get(field, "").strip() for field in _TARGET_FIELDS)


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
def load_progress() -> set:
    """Load set of already-processed website_url strings from disk."""
    if os.path.exists(_PROGRESS_FILE):
        try:
            with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("processed_urls", []))
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def save_progress(processed_urls: set):
    """Atomically persist the processed URLs set to disk."""
    dir_name = os.path.dirname(_PROGRESS_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"processed_urls": sorted(processed_urls)}, f, indent=2)
        os.replace(tmp_path, _PROGRESS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# SeleniumBase website scraper — child process function
# ---------------------------------------------------------------------------
def _website_scrape_worker(url: str, proxy_str: str, result_queue):
    """
    Child process: open the target website with SeleniumBase UC mode,
    extract homepage HTML, logo URL, and the first valid contact page HTML.

    Uses Xvfb virtual display (required for UC mode headed browser).
    Identical browser setup as google_scraper._scrape_worker.

    Puts a dict on result_queue:
        {"homepage_html": str, "contact_html": str, "logo_url": str, "ok": bool}
    """
    result = {"homepage_html": "", "contact_html": "", "logo_url": "", "ok": False}

    # Stagger startup so concurrent workers don't all hit sites simultaneously
    time.sleep(random.uniform(0, 3))

    virtual_display = None
    try:
        # Start Xvfb virtual display (required for UC mode headed browser)
        try:
            from sbvirtualdisplay import Display
            virtual_display = Display(visible=0, size=(1920, 1080))
            virtual_display.start()
        except Exception:
            pass

        # Ensure DISPLAY is set so pyautogui can be imported without error.
        # If sbvirtualdisplay failed silently above, import pyautogui raises
        # KeyError: 'DISPLAY' which SeleniumBase misreads as "not installed"
        # and attempts `pip install pyautogui` — failing with
        # "externally-managed-environment" on Debian/Ubuntu Python 3.12+.
        if "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":99"

        w, h = random.choice(_WINDOW_SIZES)
        user_agent = random.choice(_USER_AGENTS)

        chromium_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            f"--window-size={w},{h}",
            "--disable-blink-features=AutomationControlled",
        ]

        sb_kwargs = {
            "uc": True,           # undetected-chromedriver (patches navigator.webdriver)
            "headless": False,    # headed mode on Xvfb — UC mode requires a display
            "incognito": True,    # fresh cookie/storage state per session
            "driver_version": "147",
            "chromium_arg": ",".join(chromium_args),
            "agent": user_agent,
        }

        chrome_binary = _find_chrome_binary()
        if chrome_binary:
            sb_kwargs["binary_location"] = chrome_binary

        if proxy_str:
            sb_kwargs["proxy"] = proxy_str

        with SB(**sb_kwargs) as sb:
            try:
                sb.driver.set_page_load_timeout(25)
                sb.driver.set_script_timeout(25)
            except Exception:
                pass

            reconnect_time = random.uniform(3, 6)

            # --- Homepage ---
            # uc_open_with_reconnect handles Cloudflare JS challenges by
            # disconnecting ChromeDriver during the challenge, then reconnecting.
            try:
                sb.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
            except Exception:
                try:
                    sb.open(url)
                except Exception:
                    pass  # still try get_page_source — may have partial content

            # Wait for JS render — business sites need less time than Google
            time.sleep(random.uniform(4, 7))

            # Human-like scroll
            try:
                scroll_y = random.randint(200, 500)
                sb.execute_script(f"window.scrollBy(0, {scroll_y});")
                time.sleep(random.uniform(0.5, 1.5))
            except Exception:
                pass

            homepage_html = ""
            try:
                homepage_html = sb.get_page_source()
                if homepage_html:
                    homepage_html = homepage_html[:config.MAX_RAW_HTML_LENGTH]
            except Exception:
                pass

            result["homepage_html"] = homepage_html

            # Extract logo from homepage HTML while we have it
            if homepage_html:
                try:
                    result["logo_url"] = extract_logo_url(homepage_html, url)
                except Exception:
                    pass

            # --- Contact pages (SeleniumBase only) ---
            # Use a shorter page load timeout per path (15s) and limit to 4 paths
            # so the total contact-page phase takes at most ~60s, keeping the
            # full subprocess comfortably inside the 180s hard timeout.
            _CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us"]
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            contact_html = ""

            try:
                sb.driver.set_page_load_timeout(15)
                sb.driver.set_script_timeout(15)
            except Exception:
                pass

            for path in _CONTACT_PATHS:
                if contact_html:
                    break
                contact_url = base_url + path
                try:
                    sb.open(contact_url)
                    time.sleep(2)
                    src = sb.get_page_source()
                    if src and len(src) > 500:
                        src_lower = src[:500].lower()
                        if "404" not in src_lower and "not found" not in src_lower:
                            contact_html = src[:config.MAX_RAW_HTML_LENGTH]
                except Exception:
                    continue

            result["contact_html"] = contact_html
            result["ok"] = bool(homepage_html)

    except Exception:
        pass
    finally:
        if virtual_display is not None:
            try:
                virtual_display.stop()
            except Exception:
                pass

    try:
        result_queue.put(result)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Website scraper manager — subprocess + hard timeout + retries
# ---------------------------------------------------------------------------
def scrape_website_with_selenium(url: str, proxies: list, logger: logging.Logger) -> dict:
    """
    Visit a website using SeleniumBase with proxy support.

    Runs the browser in a separate process with a hard 120s timeout so dead
    proxies or unresponsive sites never hang a worker thread.
    Retries up to 5 times with exponential backoff and proxy rotation.

    Returns:
        dict with keys: homepage_html, contact_html, logo_url (empty strings on failure).
    """
    empty_result = {"homepage_html": "", "contact_html": "", "logo_url": "", "ok": False}

    for attempt in range(_MAX_RETRIES):
        # Stagger first attempt so workers don't all fire simultaneously
        if attempt == 0:
            time.sleep(random.uniform(0, 2))

        proxy = get_random_proxy(proxies)
        bridge = None
        proxy_str = None

        if proxy:
            scheme = proxy.get("scheme", "")
            if scheme.startswith("socks"):
                # SOCKS5 → local HTTP bridge (Chrome can't do SOCKS5 auth natively)
                bridge = Socks5Bridge(
                    socks_host=proxy["ip"],
                    socks_port=proxy["port"],
                    socks_user=proxy.get("username"),
                    socks_pass=proxy.get("password"),
                )
                local_port = bridge.start()
                proxy_str = f"127.0.0.1:{local_port}"
            else:
                auth = ""
                if proxy.get("username"):
                    auth = f"{proxy['username']}:{proxy['password']}@"
                proxy_str = f"{auth}{proxy['ip']}:{proxy['port']}"

        proc = None
        try:
            result_queue = multiprocessing.Queue()
            proc = multiprocessing.Process(
                target=_website_scrape_worker,
                args=(url, proxy_str, result_queue),
            )

            logger.info(f"Visiting (attempt {attempt + 1}/{_MAX_RETRIES}): {url}")
            proc.start()

            # IMPORTANT: read from queue BEFORE joining the process.
            # The subprocess puts up to 200KB of HTML on the queue.
            # On Linux the OS pipe buffer is only ~65KB — if we call
            # proc.join() first, the subprocess blocks on queue.put()
            # waiting for the reader, while the parent blocks on
            # proc.join() waiting for the subprocess to exit.  Deadlock.
            # Fix: drain the queue with a timeout, then clean up the proc.
            worker_result = dict(empty_result)
            timed_out = False
            try:
                r = result_queue.get(timeout=_SCRAPE_TIMEOUT)
                if isinstance(r, dict):
                    worker_result = r
            except Exception:
                # Queue.Empty raised on timeout — subprocess is still running
                timed_out = True

            # Now it is safe to join (queue is drained, no deadlock possible)
            proc.join(timeout=10)

            if timed_out or proc.is_alive():
                logger.warning(
                    f"Website scrape timed out after {_SCRAPE_TIMEOUT}s "
                    f"for {url} (attempt {attempt + 1}/{_MAX_RETRIES}), killing & retrying"
                )
                _safe_kill_process(proc)
                backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                time.sleep(backoff)
                continue

            # SOCKS5 auth failure — rotate proxy
            if bridge and bridge.auth_failure_count > 0:
                logger.warning(
                    f"SOCKS5 auth failed ({bridge.auth_failure_count} rejections) "
                    f"for {url} on attempt {attempt + 1}/{_MAX_RETRIES}, rotating proxy"
                )
                backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                time.sleep(backoff)
                continue

            if worker_result.get("ok") or worker_result.get("homepage_html"):
                logger.info(f"Successfully scraped: {url}")
                return worker_result

            # Empty result — retry with backoff
            logger.warning(
                f"Empty result for {url} "
                f"(attempt {attempt + 1}/{_MAX_RETRIES}), retrying..."
            )
            backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
            time.sleep(backoff)

        except Exception as e:
            logger.error(
                f"Exception during website scrape for {url} "
                f"(attempt {attempt + 1}/{_MAX_RETRIES}): {type(e).__name__}: {e}"
            )
            logger.error(traceback.format_exc())
            backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
            time.sleep(backoff)
        finally:
            if proc and proc.is_alive():
                _safe_kill_process(proc)
            if bridge:
                try:
                    bridge.stop()
                except Exception:
                    pass

    logger.warning(f"All {_MAX_RETRIES} attempts exhausted for {url}")
    return empty_result


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def worker_fill_row(
    row_index: int,
    row: dict,
    rows: list,
    fieldnames: list,
    csv_filepath: str,
    processed_urls: set,
    proxies: list,
    logger: logging.Logger,
) -> str:
    """
    Worker: scrape the row's website_url, run AI extraction, then fill
    any currently-empty target fields back into the shared rows list.

    Merge rule: only overwrites a field if the existing value is empty
    AND the AI returned a non-empty value — never clobbers existing data.

    Saves the full CSV atomically after each successful row update.
    """
    if _shutdown_event.is_set():
        return f"SHUTDOWN: row {row_index}"

    url = row.get("website_url", "").strip()
    if not url:
        return f"SKIP (no URL): row {row_index}"

    label = f"row {row_index} | {url}"
    logger.info(f"[Worker] Processing {label}")

    # Step 1: Scrape homepage + contact page using SeleniumBase
    scraped = scrape_website_with_selenium(url, proxies, logger)
    contact_html = scraped.get("contact_html", "")

    # Step 2: Send scraped HTML to AI extractor (same function as main scraper)
    business_data = extract_business_data(
        homepage_html=scraped["homepage_html"],
        contact_html=contact_html,
        logo_url=scraped["logo_url"],
        website_url=url,
    )

    # Step 3: Merge — only fill fields that are currently empty
    fields_filled = []
    with _csv_lock:
        for field in _TARGET_FIELDS:
            existing = rows[row_index].get(field, "").strip()
            new_val = business_data.get(field, "").strip()
            if not existing and new_val:
                rows[row_index][field] = new_val
                fields_filled.append(field)

        # Step 4: Atomically save the full CSV after updating this row
        if fields_filled:
            try:
                save_csv(rows, fieldnames, csv_filepath)
                logger.info(
                    f"[Worker] Updated {label} — filled: {', '.join(fields_filled)}"
                )
            except Exception as e:
                logger.error(f"[Worker] CSV save failed for {label}: {e}")

    # Step 5: Mark URL as done in the progress set and persist
    with _progress_lock:
        processed_urls.add(url)
        try:
            save_progress(processed_urls)
        except Exception as e:
            logger.error(f"[Worker] Progress save failed for {label}: {e}")

    if not fields_filled:
        logger.info(
            f"[Worker] No new data for {label} "
            f"(AI returned empty or all fields already populated)"
        )
        return f"NO DATA: {label}"

    # Polite delay between visits
    time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

    return f"DONE ({len(fields_filled)} fields): {label}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fill missing fields in hvac_companies_cleaned.csv using SeleniumBase.",
    )
    parser.add_argument(
        "--workers", type=int, default=config.NUM_WORKERS,
        help=f"Number of concurrent worker threads (default: {config.NUM_WORKERS})",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Test mode: process only the first 2 rows that need filling (1 worker)",
    )
    parser.add_argument(
        "--csv", default=_DEFAULT_CSV, metavar="PATH",
        help=f"Path to CSV file (default: hvac_companies_cleaned.csv)",
    )
    args = parser.parse_args()

    num_workers = 1 if args.test else max(1, args.workers)
    csv_filepath = os.path.abspath(args.csv)

    logger = _setup_logging()
    logger.info("=" * 60)
    logger.info(f"fill_missing.py — Starting ({num_workers} workers)")
    if args.test:
        logger.info("TEST MODE: will process first 2 rows only")
    logger.info("=" * 60)

    # --- Memory cleanup daemon (same as main.py — prevents OOM on long runs) ---
    mem_cleaner = threading.Thread(
        target=_memory_cleanup_loop, args=(logger,), daemon=True, name="mem-cleaner"
    )
    mem_cleaner.start()
    logger.info(f"Memory cleanup daemon started (interval: {_MEM_CLEANUP_INTERVAL}s)")

    # --- Kill orphaned Chrome processes left by a previous run ---
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

    # --- Load proxies ---
    proxies = load_proxies()
    if not proxies:
        logger.warning("No proxies loaded — running without proxies (not recommended).")
    else:
        logger.info(f"Loaded {len(proxies)} proxies")

    # --- Load CSV ---
    if not os.path.exists(csv_filepath):
        logger.error(f"CSV file not found: {csv_filepath}")
        sys.exit(1)

    rows, fieldnames = load_csv(csv_filepath)
    logger.info(f"Loaded {len(rows)} rows from {csv_filepath}")

    # --- Identify rows that need processing ---
    todo = [
        (i, row) for i, row in enumerate(rows)
        if row_needs_processing(row)
    ]
    logger.info(f"Rows with at least one empty target field: {len(todo)}")

    if not todo:
        logger.info("Nothing to do — all rows are already complete.")
        return

    # --- Load progress and filter already-done URLs ---
    processed_urls = load_progress()
    if processed_urls:
        logger.info(
            f"Resuming: {len(processed_urls)} URL(s) already processed in previous runs"
        )

    todo = [
        (i, row) for i, row in todo
        if row.get("website_url", "").strip() not in processed_urls
    ]
    logger.info(f"Remaining rows to process: {len(todo)}")

    if not todo:
        logger.info("All rows were already processed in a previous run.")
        return

    # --- Test mode: cap to first 2 rows ---
    if args.test:
        todo = todo[:2]
        logger.info(f"TEST MODE: capped to {len(todo)} row(s)")

    # --- Ctrl+C handler ---
    original_sigint = signal.getsignal(signal.SIGINT)

    def _signal_handler(signum, frame):
        if _shutdown_event.is_set():
            logger.warning("Force shutdown!")
            sys.exit(1)
        logger.warning("Shutdown requested (Ctrl+C). Finishing in-flight work...")
        print("\nShutting down gracefully... (press Ctrl+C again to force)")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)

    completed_count = 0
    no_data_count = 0
    failed_count = 0
    total = len(todo)

    try:
        with ThreadPoolExecutor(
            max_workers=num_workers, thread_name_prefix="fill"
        ) as executor:
            future_to_idx = {}

            for row_index, row in todo:
                if _shutdown_event.is_set():
                    break

                future = executor.submit(
                    worker_fill_row,
                    row_index, row, rows, fieldnames, csv_filepath,
                    processed_urls, proxies, logger,
                )
                future_to_idx[future] = row_index

                # Drain completed futures to keep the dict memory-bounded
                if len(future_to_idx) >= num_workers * 4:
                    done_futures = [f for f in future_to_idx if f.done()]
                    for f in done_futures:
                        idx = future_to_idx.pop(f)
                        try:
                            res = f.result()
                            if res.startswith("DONE"):
                                completed_count += 1
                            elif res.startswith("NO DATA"):
                                no_data_count += 1
                            else:
                                failed_count += 1
                            done_so_far = completed_count + no_data_count + failed_count
                            logger.info(
                                f"[Progress] {done_so_far}/{total} — {res}"
                            )
                        except Exception as e:
                            failed_count += 1
                            logger.error(
                                f"Worker crash row {idx}: "
                                f"{type(e).__name__}: {e}"
                            )
                            logger.error(traceback.format_exc())

            # Drain remaining futures
            for f in as_completed(future_to_idx):
                idx = future_to_idx[f]
                try:
                    res = f.result()
                    if res.startswith("DONE"):
                        completed_count += 1
                    elif res.startswith("NO DATA"):
                        no_data_count += 1
                    else:
                        failed_count += 1
                    done_so_far = completed_count + no_data_count + failed_count
                    logger.info(f"[Progress] {done_so_far}/{total} — {res}")
                except Exception as e:
                    failed_count += 1
                    logger.error(
                        f"Worker crash row {idx}: {type(e).__name__}: {e}"
                    )
                    logger.error(traceback.format_exc())

                if _shutdown_event.is_set():
                    logger.info("Cancelling remaining futures...")
                    for pending in future_to_idx:
                        pending.cancel()
                    break

    finally:
        # Always flush AI content cache on exit (saves tokens on next run)
        save_content_cache()
        signal.signal(signal.SIGINT, original_sigint)

    logger.info("=" * 60)
    logger.info(
        f"fill_missing.py — Done | "
        f"Fields filled: {completed_count} rows | "
        f"No new data: {no_data_count} rows | "
        f"Failed: {failed_count} rows"
    )
    logger.info("=" * 60)

    if args.test:
        print("\n[TEST] Check fill_missing.log for full details.")
        print(f"[TEST] Filled: {completed_count} | No data: {no_data_count} | Failed: {failed_count}")


if __name__ == "__main__":
    main()
