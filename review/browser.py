"""SeleniumBase browser factory + per-site retry wrapper.

Each call to `run_with_retries(scraper, row, proxies, ...)` runs the scraper's
search + scrape in a child PROCESS (multiprocessing.Process) so:
  - a hung Chrome is killed via proc.kill() after SITE_HARD_TIMEOUT
  - blocked attempts get a fresh proxy + fresh Chrome on retry
  - SOCKS5 auth failures (counted by Socks5Bridge) trigger proxy rotation
"""
import glob
import logging
import multiprocessing
import os
import random
import sys
import time

from . import config
from . import proxies as proxies_mod
from .base_scraper import (
    STATUS_DONE,
    STATUS_NOT_FOUND,
    STATUS_BLOCKED,
    STATUS_ERROR,
    empty_result,
)

logger = logging.getLogger("review")

# Make parent project importable in subprocess
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)


# --- Chrome binary detection (mirrors google_scraper.py:_find_chrome_binary) ---
def _find_chrome_binary():
    patterns = [
        "/usr/local/lib/python3.12/dist-packages/seleniumbase/drivers/chrome-linux64/chrome",
        os.path.expanduser("~/.cache/selenium/chrome/linux64/*/chrome"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for pattern in patterns:
        for match in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(match) and os.path.getsize(match) > 1024:
                return match
    return None


_CHROME_BINARY = _find_chrome_binary()


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
_WINDOW_SIZES = [
    (1280, 800), (1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080),
]


def _safe_kill_process(proc):
    try:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
    except Exception:
        pass


def _site_worker(scraper_module_path, scraper_class_name, row, proxy_str, result_queue):
    """Child-process entrypoint. Imports the scraper class, opens Chrome with
    the supplied proxy, runs scraper.run(sb, row), puts result on the queue.
    """
    # Stagger so 17 simultaneous workers don't all fire in the same millisecond
    time.sleep(random.uniform(*config.WORKER_STAGGER_SECONDS))

    result = empty_result(STATUS_ERROR)
    virtual_display = None

    try:
        # Dynamic import — avoids circular imports during module loading
        import importlib
        mod = importlib.import_module(scraper_module_path)
        cls = getattr(mod, scraper_class_name)
        scraper = cls()

        from seleniumbase import SB

        w, h = random.choice(_WINDOW_SIZES)
        ua = random.choice(_USER_AGENTS)
        chromium_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            f"--window-size={w},{h}",
            "--disable-blink-features=AutomationControlled",
            # Reduce memory per Chrome — 17 in parallel adds up
            "--js-flags=--max-old-space-size=512",
            "--disable-extensions",
            "--disable-plugins",
            "--mute-audio",
        ]

        # Virtual display is now started by SB itself (xvfb=True kwarg below).

        sb_kwargs = {
            "uc": True,
            "undetectable": True,        # extra UC patches
            "headless": False,
            "incognito": True,
            "locale": "en",
            "ad_block": True,            # cuts ad-tracker noise + speeds page loads
            "chromium_arg": ",".join(chromium_args),
            "agent": ua,
            "xvfb": True,                # virtual display — required for CDP mode
        }
        if _CHROME_BINARY:
            sb_kwargs["binary_location"] = _CHROME_BINARY
        if proxy_str:
            sb_kwargs["proxy"] = proxy_str

        with SB(**sb_kwargs) as sb:
            try:
                sb.driver.set_page_load_timeout(45)
                sb.driver.set_script_timeout(45)
            except Exception:
                pass
            result = scraper.run(sb, row)
    except Exception as e:
        try:
            from . import config as _c  # noqa
        except Exception:
            pass
        logging.getLogger("review").warning(
            f"[{scraper_class_name}] worker raised: {type(e).__name__}: {e}"
        )
        result = empty_result(STATUS_ERROR)
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


def _build_proxy_string(proxy):
    """Returns (proxy_str, bridge_obj_or_None) for use in SB kwargs.

    The bridge is started in THIS process (the worker thread, not the child
    Chrome process) and shared with the subprocess via 127.0.0.1:PORT.
    """
    if not proxy:
        return None, None
    scheme = proxy.get("scheme", "")
    if scheme.startswith("socks"):
        from socks_bridge import Socks5Bridge
        bridge = Socks5Bridge(
            socks_host=proxy["ip"],
            socks_port=proxy["port"],
            socks_user=proxy.get("username"),
            socks_pass=proxy.get("password"),
        )
        local_port = bridge.start()
        return f"127.0.0.1:{local_port}", bridge
    auth = ""
    if proxy.get("username"):
        auth = f"{proxy['username']}:{proxy['password']}@"
    return f"{auth}{proxy['ip']}:{proxy['port']}", None


def run_with_retries(scraper, row, proxies, max_attempts=None):
    """Run one site scraper with proxy rotation + hard timeout.

    Returns one of the standard result dicts (status in done/not_found/blocked/error).
    """
    site_id = scraper.SITE_ID
    cls = scraper.__class__
    module_path = cls.__module__       # e.g. "review.sites.yelp"
    class_name = cls.__name__
    n = max_attempts or scraper.MAX_RETRIES

    used_proxy_ids = set()
    last_status = STATUS_BLOCKED  # default if all attempts fail

    for attempt in range(1, n + 1):
        proxy = proxies_mod.pick_proxy(proxies, exclude=used_proxy_ids)
        if proxy:
            used_proxy_ids.add(proxies_mod.proxy_id(proxy))

        proxy_str, bridge = None, None
        try:
            proxy_str, bridge = _build_proxy_string(proxy)
        except Exception as e:
            logger.warning(f"[{site_id}] bridge start failed: {e}")
            proxy_str, bridge = None, None

        proc = None
        try:
            result_queue = multiprocessing.Queue()
            proc = multiprocessing.Process(
                target=_site_worker,
                args=(module_path, class_name, row, proxy_str, result_queue),
                name=f"{site_id}-attempt{attempt}",
            )
            logger.info(f"[{site_id}] attempt {attempt}/{n} starting (proxy={'yes' if proxy else 'no'})")
            proc.start()
            proc.join(timeout=config.SITE_HARD_TIMEOUT)

            if proc.is_alive():
                logger.warning(f"[{site_id}] hard-timeout {config.SITE_HARD_TIMEOUT}s — killing & retrying")
                _safe_kill_process(proc)
                last_status = STATUS_BLOCKED
                _backoff(attempt)
                continue

            # Process exited — read result
            worker_result = empty_result(STATUS_ERROR)
            try:
                if not result_queue.empty():
                    worker_result = result_queue.get_nowait()
            except Exception:
                pass

            # Detect SOCKS5 auth failure → rotate proxy, don't count as 'real' attempt
            if bridge and bridge.auth_failure_count > 0:
                logger.warning(
                    f"[{site_id}] SOCKS5 auth failures={bridge.auth_failure_count} — rotating proxy"
                )
                last_status = STATUS_BLOCKED
                _backoff(attempt)
                continue

            status = worker_result.get("status", STATUS_ERROR)

            if status == STATUS_DONE:
                rev_count = len(worker_result.get("reviews") or [])
                logger.info(
                    f"[{site_id}] done — rating={worker_result.get('rating')} reviews={rev_count}"
                )
                return worker_result

            if status == STATUS_NOT_FOUND:
                logger.info(f"[{site_id}] business not found — terminal")
                return worker_result

            # ERROR or BLOCKED → retry
            last_status = status if status in (STATUS_BLOCKED, STATUS_ERROR) else STATUS_ERROR
            logger.warning(f"[{site_id}] attempt {attempt}/{n} returned status={status} — retrying")
            _backoff(attempt)

        except Exception as e:
            logger.error(f"[{site_id}] orchestration error attempt {attempt}/{n}: {type(e).__name__}: {e}")
            if proc is not None:
                _safe_kill_process(proc)
            last_status = STATUS_ERROR
            _backoff(attempt)
        finally:
            if bridge:
                try:
                    bridge.stop()
                except Exception:
                    pass

    logger.error(f"[{site_id}] exhausted {n} attempts — final status={last_status}")
    return empty_result(STATUS_BLOCKED if last_status == STATUS_BLOCKED else STATUS_ERROR)


def _backoff(attempt):
    delay = min(3 * (2 ** (attempt - 1)), 30)
    time.sleep(delay)
