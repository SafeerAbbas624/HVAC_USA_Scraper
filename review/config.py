"""Review-scraper configuration: paths, knobs, site registry helpers."""
import os
import logging

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_BASE_DIR)

# --- File paths ---
INPUT_CSV = os.path.join(_PROJECT_DIR, "hvac_companies_cleaned.csv")
OUTPUT_CSV = os.path.join(_BASE_DIR, "review_output.csv")
PROGRESS_FILE = os.path.join(_BASE_DIR, "review_progress.json")
LOG_FILE = os.path.join(_BASE_DIR, "review.log")
PROXIES_FILE = os.path.join(_PROJECT_DIR, "proxies.txt")

# Test artifacts (used by test_review.py)
TEST_OUTPUT_CSV = os.path.join(_BASE_DIR, "test_output.csv")
TEST_PROGRESS_FILE = os.path.join(_BASE_DIR, "test_progress.json")
TEST_LOG_FILE = os.path.join(_BASE_DIR, "test.log")

# --- Scraping knobs ---
MAX_REVIEWS_PER_SITE = 100
SITES_PER_ROW = 5                     # 5 active scrapers — google_maps, yelp,
                                      # bbb, angi, homeadvisor — run in parallel.
MAX_PARALLEL_ROWS = 10                # rows processed concurrently. Total
                                      # concurrent Chrome subprocesses =
                                      # MAX_PARALLEL_ROWS × SITES_PER_ROW.
                                      #
                                      # History on this 18-core box:
                                      #   4  → 36.7 rows/h
                                      #   8  → 113  rows/h (load ~16/18)
                                      #  10  → ?    rows/h (current — load
                                      #       expected ~22, mild over-
                                      #       subscribe with HT)
                                      #  12  → 73   rows/h (load 56/18 —
                                      #       too many Chrome helper procs
                                      #       saturating CPU)
                                      # To go above 10–12: more cores or
                                      # split Chrome workers onto a
                                      # separate machine.
ROW_HARD_TIMEOUT = 2400               # 40 min — must exceed worst-case
                                      # (heavy blocker 10 retries × 240s = 40 min).
SITE_HARD_TIMEOUT = 120               # per-site Chrome subprocess timeout
                                      # Lowered 240→120s because google_maps
                                      # was spending 5×240s = 20 min on a
                                      # handful of slow rows. Typical run
                                      # time per site is 30-60s, so 120s is
                                      # 2x typical with headroom. Sites that
                                      # truly need >120s (rare) just exhaust
                                      # retries faster, which Phase 2 picks up.
DEFAULT_MAX_RETRIES = 5               # per-site attempt count
HEAVY_BLOCKER_RETRIES = 10            # Yelp/BBB/Angi — DataDome/Cloudflare
                                      # block ~50%+ of webshare IPs, so we
                                      # need many retries to land on a clean IP.
WORKER_STAGGER_SECONDS = (0, 5)       # random startup delay per site (min, max)
RECONNECT_TIME_RANGE = (5, 9)         # uc_open_with_reconnect range

# --- Cleanup daemon ---
MEM_CLEANUP_INTERVAL = 300            # 5 minutes — orphan Chrome/Xvfb reaper

# --- Retry sweep (Phase 2) ---
RETRY_MAX_PASSES = 3
RETRY_INTERPASS_SLEEP = 60            # seconds between retry passes

# --- Block detection signals ---
BLOCK_SIGNAL_STRINGS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "attention required! | cloudflare",
    "px-captcha",
    "perimeterx",
    "verify you are a human",
    "verify you are human",
    "are you a human",
    "access denied",
    "unusual activity",
    "automated requests",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "blocked by",
    "request unsuccessful",
    "cloudflare",
)
MIN_VALID_BODY_BYTES = 1024


def setup_logging(log_file: str = None, console_level=logging.INFO):
    """Configure the 'review' logger with file + console handlers."""
    log_file = log_file or LOG_FILE
    logger = logging.getLogger("review")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(console_level)

    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] %(message)s"
    )
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def build_output_headers(input_headers, site_registry):
    """Build the full output CSV header list = _row_idx + input cols + per-site cols."""
    headers = ["_row_idx"] + list(input_headers)
    for site in site_registry:
        headers.append(f"{site.SITE_ID}_rating")
        if site.HAS_REVIEWS:
            headers.append(f"{site.SITE_ID}_reviews")
    return headers
