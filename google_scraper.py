"""
Google Search results scraper using SeleniumBase with authenticated proxy support.

Uses multiprocessing to enforce a hard timeout on browser operations,
so dead proxies can never hang the scraper indefinitely.
"""
import random
import time
import logging
import multiprocessing
import glob
import os
from urllib.parse import urlparse

from seleniumbase import SB

import config
from socks_bridge import Socks5Bridge


def _find_chrome_binary():
    """Find the Chrome binary in the Selenium cache or system paths."""
    patterns = [
        # SeleniumBase's own chrome-for-testing install (most reliable on this server)
        '/usr/local/lib/python3.12/dist-packages/seleniumbase/drivers/chrome-linux64/chrome',
        os.path.expanduser('~/.cache/selenium/chrome/linux64/*/chrome'),
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        for match in matches:
            # Skip zero-byte or non-executable placeholders
            if os.path.isfile(match) and os.path.getsize(match) > 1024:
                return match
    return None

_CHROME_BINARY = _find_chrome_binary()

logger = logging.getLogger("scraper")


def load_proxies(filepath: str = None) -> list:
    """
    Load proxies from the proxies file.

    Supported formats per line:
      - URL:   socks5://user:pass@host:port  or  http://user:pass@host:port
      - Legacy: IP:PORT:USERNAME:PASSWORD
    Returns list of dicts.  URL-format dicts carry a 'scheme' key.
    """
    filepath = filepath or config.PROXIES_FILE
    proxies = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # URL-format: scheme://user:pass@host:port
                if "://" in line:
                    try:
                        parsed = urlparse(line)
                        proxies.append({
                            "scheme": parsed.scheme,        # socks5, http, …
                            "ip": parsed.hostname,
                            "port": str(parsed.port),
                            "username": parsed.username or "",
                            "password": parsed.password or "",
                        })
                    except Exception as e:
                        logger.warning(f"Could not parse proxy URL: {line} — {e}")
                else:
                    # Legacy format: IP:PORT:USERNAME:PASSWORD
                    parts = line.split(":")
                    if len(parts) == 4:
                        proxies.append({
                            "ip": parts[0],
                            "port": parts[1],
                            "username": parts[2],
                            "password": parts[3],
                        })
    except FileNotFoundError:
        logger.error(f"Proxies file not found: {filepath}")
    return proxies


def get_random_proxy(proxies: list) -> dict:
    """Return a random proxy from the list."""
    if not proxies:
        return None
    return random.choice(proxies)


def format_proxy_for_seleniumbase(proxy: dict) -> str:
    """
    Format a proxy dict into SeleniumBase proxy string.

    HTTP proxies  → username:password@ip:port   (SeleniumBase creates auth extension)
    SOCKS5 proxies → handled via local bridge, returns None (bridge sets up separately)
    """
    if not proxy:
        return None
    scheme = proxy.get("scheme", "")
    if scheme.startswith("socks"):
        # SOCKS5 proxies are handled by the bridge — return None here;
        # the caller must start/use the bridge separately.
        return None
    auth = ""
    if proxy.get("username"):
        auth = f"{proxy['username']}:{proxy['password']}@"
    host_port = f"{proxy['ip']}:{proxy['port']}"
    return f"{auth}{host_port}"


def is_excluded_url(url: str) -> bool:
    """Check if a URL belongs to an excluded/social media domain or non-US TLD."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()

        # Remove 'www.' prefix for matching
        if domain.startswith("www."):
            domain = domain[4:]

        # Exclude non-US country TLDs (we target US businesses only)
        non_us_tlds = (
            ".ca", ".uk", ".co.uk", ".au", ".in", ".de", ".fr", ".mx",
            ".es", ".it", ".nl", ".pt", ".pl", ".se", ".no", ".dk", ".fi",
            ".ch", ".at", ".be", ".br", ".ar", ".cl", ".co", ".pe", ".ve",
            ".nz", ".co.nz", ".za", ".co.za", ".sg", ".hk", ".jp", ".cn",
            ".kr", ".ru", ".tr", ".sa", ".ae", ".pk", ".ng", ".gh", ".ke",
            ".ie", ".cz", ".sk", ".hu", ".ro", ".bg", ".rs", ".hr", ".si",
        )
        for tld in non_us_tlds:
            if domain.endswith(tld):
                return True

        # Exclude URLs with foreign language path segments
        # e.g. /de/, /es/, /fr/, /sv-se/ etc.
        foreign_path_segments = (
            "/de/", "/es/", "/fr/", "/it/", "/nl/", "/pt/", "/pl/",
            "/sv-se/", "/de-de/", "/es-es/", "/fr-fr/", "/en-gb/",
            "/en-au/", "/en-ca/", "/en-in/",
        )
        for seg in foreign_path_segments:
            if seg in path:
                return True

        # Match excluded domains by exact equality or proper subdomain only
        # (e.g. "x.com" must match "x.com" or "*.x.com", not "abcx.com").
        # The leading "." entries (".gov") still match suffixes correctly.
        for excluded in config.EXCLUDED_DOMAINS:
            if excluded.startswith("."):
                if domain.endswith(excluded):
                    return True
            elif domain == excluded or domain.endswith("." + excluded):
                return True
    except Exception:
        return True
    return False


def _add_url(found_urls: list, href: str):
    """Normalize and add a URL if it passes filters."""
    if not href or not href.startswith("http"):
        return
    if is_excluded_url(href):
        return
    parsed = urlparse(href)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if clean_url not in found_urls:
        found_urls.append(clean_url)


def _extract_urls_from_page(sb) -> list:
    """
    Extract organic result URLs from a loaded Google search page.

    Uses multiple CSS selector strategies to capture all organic results.
    Returns a list of cleaned, filtered URLs.
    """
    found_urls = []

    # --- Strategy 1 (primary): h3 inside #rso direct children ---
    # Google 2025+ layout: div.g is gone; results sit under #rso > div
    try:
        h3_elems = sb.find_elements("css selector", "#rso > div a h3")
        for h3 in h3_elems:
            try:
                a_tag = h3.find_element("xpath", "./ancestor::a")
                href = a_tag.get_attribute("href")
                _add_url(found_urls, href)
            except Exception:
                continue
    except Exception:
        pass

    # --- Strategy 2: div#search a h3 (broader, catches nested layouts) ---
    try:
        h3_elems = sb.find_elements("css selector", "div#search a h3")
        for h3 in h3_elems:
            try:
                a_tag = h3.find_element("xpath", "./ancestor::a")
                href = a_tag.get_attribute("href")
                _add_url(found_urls, href)
            except Exception:
                continue
    except Exception:
        pass

    # --- Strategy 3: div.g a with h3 (legacy, kept for older layouts) ---
    try:
        elems = sb.find_elements("css selector", "div#search div.g a[href]")
        for elem in elems:
            href = elem.get_attribute("href")
            try:
                elem.find_element("css selector", "h3")
                _add_url(found_urls, href)
            except Exception:
                continue
    except Exception:
        pass

    # --- Strategy 4: cite-based extraction (Google shows domain in cite) ---
    try:
        cites = sb.find_elements("css selector", "div#search cite")
        for cite in cites:
            cite_text = cite.text.strip()
            if cite_text and "." in cite_text:
                try:
                    a_tag = cite.find_element("xpath", "./ancestor::a[@href]")
                    href = a_tag.get_attribute("href")
                    _add_url(found_urls, href)
                except Exception:
                    pass
    except Exception:
        pass

    # --- Strategy 5: Google 2025/2026 layout — jsname / data-ved anchors ---
    try:
        elems = sb.find_elements("css selector", "#rso a[jsname][href], #rso a[data-ved][href]")
        for elem in elems:
            href = elem.get_attribute("href")
            _add_url(found_urls, href)
    except Exception:
        pass

    # --- Strategy 6: Always-on broad sweep of ALL links in #rso ---
    # Runs regardless of other strategies to catch any missed results.
    # Filters out Google-internal URLs automatically via is_excluded_url.
    try:
        elems = sb.find_elements("css selector", "#rso a[href]")
        for elem in elems:
            href = elem.get_attribute("href")
            if href and href.startswith("http") and "google.com" not in href:
                _add_url(found_urls, href)
    except Exception:
        pass

    # --- Strategy 7: search-wide sweep as final safety net ---
    try:
        elems = sb.find_elements("css selector", "div#search a[href]")
        for elem in elems:
            href = elem.get_attribute("href")
            if href and href.startswith("http") and "google.com" not in href:
                _add_url(found_urls, href)
    except Exception:
        pass

    return found_urls


# Randomised Chrome user-agents (Windows/Mac/Linux, Chrome 122-124)
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Randomised window sizes to avoid uniform fingerprinting across workers
_WINDOW_SIZES = [
    (1280, 720), (1280, 800), (1366, 768), (1440, 900),
    (1536, 864), (1600, 900), (1920, 1080),
]

# Strings that indicate Google is blocking us (CAPTCHA / unusual traffic)
_BLOCK_SIGNALS = [
    "unusual traffic",
    "our systems have detected",
    "recaptcha",
    "sorry/index",
    "www.google.com/sorry",
    "captcha",
    "automated queries",
]


def _detect_google_block(sb) -> bool:
    """
    Check if the current page is a Google CAPTCHA / block page.

    Returns True if we are blocked, False if the page looks like
    normal search results.
    """
    try:
        # Check URL first — Google redirects to /sorry on blocks
        current_url = sb.get_current_url().lower()
        if "/sorry" in current_url or "captcha" in current_url:
            return True

        # Check page title
        title = sb.get_title().lower()
        for signal in _BLOCK_SIGNALS:
            if signal in title:
                return True

        # Check page source (quick substring scan — don't parse DOM)
        page_src = sb.get_page_source()[:5000].lower()
        for signal in _BLOCK_SIGNALS:
            if signal in page_src:
                return True
    except Exception:
        pass
    return False


def _scrape_worker(search_url: str, proxy_str: str, result_queue):
    """
    Worker function that runs in a child process.

    Opens a browser, navigates to Google, extracts URLs, and puts results
    on the multiprocessing queue.  Receives a ready-to-use proxy string
    (bridge is managed by the caller).

    Puts a dict on the queue:
        {"urls": [...], "blocked": True/False}
    """
    # Stagger startup so 20 concurrent workers don't all hit Google at the
    # exact same millisecond — a strong bot-traffic signal.
    time.sleep(random.uniform(0, 10))

    result = {"urls": [], "blocked": False}
    # Defined BEFORE the outer try so the cleanup block at the end can always
    # reference it — even if the try body throws before assigning to it.
    virtual_display = None
    try:
        w, h = random.choice(_WINDOW_SIZES)
        user_agent = random.choice(_USER_AGENTS)

        # Build chromium_arg — comma-separated list as SeleniumBase expects
        chromium_args = [
            "--no-sandbox",                    # required when running as root on Linux
            "--disable-dev-shm-usage",         # avoids /dev/shm OOM in containers
            "--disable-gpu",                   # headless stability on Linux servers
            "--disable-software-rasterizer",   # reduces CPU on virtual display
            f"--window-size={w},{h}",          # randomise viewport fingerprint
            "--disable-blink-features=AutomationControlled",  # hide automation flag
        ]

        # Pre-start an Xvfb virtual display so that when SeleniumBase's UC mode
        # init tries to import PyAutoGUI, os.environ['DISPLAY'] is already set.
        # Without this, mouseinfo crashes on import with KeyError:'DISPLAY',
        # SeleniumBase fails to detect PyAutoGUI, and uc_open_with_reconnect hangs.
        try:
            from sbvirtualdisplay import Display
            virtual_display = Display(visible=0, size=(1920, 1080))
            virtual_display.start()
        except Exception:
            pass

        sb_kwargs = {
            "uc": True,            # undetected-chromedriver mode (patches navigator.webdriver)
            "headless": False,     # headed mode on virtual display — UC mode requires a display
            "incognito": True,     # fresh cookie/storage state per session
            "driver_version": "147",           # pin to installed Chrome 147
            "chromium_arg": ",".join(chromium_args),
            "agent": user_agent,
        }

        if _CHROME_BINARY:
            sb_kwargs["binary_location"] = _CHROME_BINARY

        if proxy_str:
            sb_kwargs["proxy"] = proxy_str

        with SB(**sb_kwargs) as sb:
            # Short page load timeout — fail fast and let the 180s hard kill
            # handle truly hung Chrome processes.
            try:
                sb.driver.set_page_load_timeout(30)
                sb.driver.set_script_timeout(30)
            except Exception:
                pass

            # Randomise reconnect time: during disconnect ChromeDriver is
            # detached so Google's JS sees a real user browsing.
            reconnect_time = random.uniform(7, 12)

            try:
                sb.uc_open_with_reconnect(search_url, reconnect_time=reconnect_time)
            except Exception:
                sb.open(search_url)

            # 8–12s wait mirrors what the reference SERP scraper uses —
            # long enough for JS-heavy pages to fully render.
            time.sleep(random.uniform(8, 12))

            # Check if Google is blocking us (CAPTCHA / unusual-traffic page).
            # If blocked, mark for retry — uc_gui_handle_captcha() could be
            # added here if needed (requires PyAutoGUI + working Xvfb display).
            if _detect_google_block(sb):
                result["blocked"] = True
                result_queue.put(result)
                return

            # Human-like scroll — real users scroll to see results
            try:
                scroll_y = random.randint(200, 500)
                sb.execute_script(f"window.scrollBy(0, {scroll_y});")
                time.sleep(random.uniform(0.5, 1.5))
            except Exception:
                pass

            # Wait for search results to load
            for wait_sel in ["div#search", "#rso", "#rso > div a h3"]:
                try:
                    sb.wait_for_element(wait_sel, timeout=15)
                    break
                except Exception:
                    continue

            result["urls"] = _extract_urls_from_page(sb)

    except Exception:
        pass

    # Stop the virtual display when done
    if virtual_display is not None:
        try:
            virtual_display.stop()
        except Exception:
            pass

    try:
        result_queue.put(result)
    except Exception:
        pass


# Hard timeout for each Google scraping attempt (seconds).
# Increased from 150 to account for the 0-10s worker stagger and
# longer reconnect_time (up to 12s) added for better undetection.
_SCRAPE_TIMEOUT = 180
_MAX_RETRIES = 7
# Backoff between retries (seconds): base * 2^attempt, capped at max
_BACKOFF_BASE = 3
_BACKOFF_MAX = 30


def _safe_kill_process(proc):
    """Safely terminate and kill a multiprocessing Process."""
    try:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
    except Exception:
        pass


def scrape_google_page(search_url: str, proxies: list) -> dict:
    """
    Scrape a single Google Search results page.

    Runs the browser in a separate process with a hard timeout.
    Retries with a different proxy (rotating IP) if blocked or failed.
    Uses exponential backoff between retries.

    Returns dict:
        {"urls": [...], "status": "ok" | "blocked" | "empty" | "error"}
    """
    blocked_count = 0

    for attempt in range(_MAX_RETRIES):
        # Stagger the first attempt per-combo so workers don't all fire
        # simultaneously at the start of each retry cycle.
        if attempt == 0:
            time.sleep(random.uniform(0, 5))

        proxy = get_random_proxy(proxies)
        bridge = None

        # Prepare proxy string for subprocess
        proxy_str = None
        if proxy:
            scheme = proxy.get("scheme", "")
            if scheme.startswith("socks"):
                # Start bridge in THIS process — shared with subprocess via localhost
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
                target=_scrape_worker,
                args=(search_url, proxy_str, result_queue),
            )

            logger.info(
                f"Navigating to: {search_url} (attempt {attempt + 1}/{_MAX_RETRIES})"
            )
            proc.start()
            proc.join(timeout=_SCRAPE_TIMEOUT)

            if proc.is_alive():
                blocked_count += 1
                logger.warning(
                    f"Google scrape timed out after {_SCRAPE_TIMEOUT}s "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES}), killing & retrying"
                )
                _safe_kill_process(proc)
                backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                time.sleep(backoff)
                continue

            # Process finished — try to get results
            worker_result = {"urls": [], "blocked": False}
            try:
                if not result_queue.empty():
                    worker_result = result_queue.get_nowait()
                    # Handle old-format returns (list) from edge cases
                    if isinstance(worker_result, list):
                        worker_result = {"urls": worker_result, "blocked": False}
            except Exception:
                pass

            # --- Blocked by Google? Retry with new proxy IP ---
            if worker_result.get("blocked"):
                blocked_count += 1
                logger.warning(
                    f"BLOCKED by Google (CAPTCHA/unusual traffic) on "
                    f"attempt {attempt + 1}/{_MAX_RETRIES} — rotating proxy"
                )
                backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                logger.info(f"Backing off {backoff}s before retry")
                time.sleep(backoff)
                continue

            # --- SOCKS5 auth failure? Treat as blocked, rotate proxy ---
            # When SOCKS5 credentials are rejected the bridge returns 502 for
            # every connection, Chrome loads nothing, and the worker returns
            # empty URLs with blocked=False.  Detect this via the bridge's
            # auth_failure_count so we retry with a different proxy instead of
            # wrongly marking the combo as completed with no results.
            if bridge and bridge.auth_failure_count > 0:
                blocked_count += 1
                logger.warning(
                    f"SOCKS5 proxy authentication failed ({bridge.auth_failure_count} "
                    f"connections rejected) on attempt {attempt + 1}/{_MAX_RETRIES} "
                    f"— rotating proxy"
                )
                backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                logger.info(f"Backing off {backoff}s before retry")
                time.sleep(backoff)
                continue

            found_urls = worker_result.get("urls", [])
            if found_urls:
                logger.info(f"Found {len(found_urls)} URLs on page")
                return {"urls": found_urls, "status": "ok"}

            logger.warning(
                f"No URLs found (attempt {attempt + 1}/{_MAX_RETRIES})"
            )
            # Short backoff even for empty results — might be transient
            backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
            time.sleep(backoff)

        except Exception as e:
            logger.error(
                f"Multiprocessing error on attempt {attempt + 1}/{_MAX_RETRIES}: "
                f"{type(e).__name__}: {e}"
            )
            if proc is not None:
                _safe_kill_process(proc)
            time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX))
        finally:
            if bridge:
                bridge.stop()

    # All retries exhausted
    if blocked_count >= _MAX_RETRIES // 2:
        logger.error(
            f"Failed after {_MAX_RETRIES} attempts ({blocked_count} blocked): {search_url}"
        )
        return {"urls": [], "status": "blocked"}

    logger.error(f"Failed to scrape Google page after {_MAX_RETRIES} attempts: {search_url}")
    return {"urls": [], "status": "empty"}

