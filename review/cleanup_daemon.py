"""Periodic Chrome/Xvfb orphan reaper — adapted from /var/HVAC/main.py:51-99.

Runs every MEM_CLEANUP_INTERVAL (300s = 5 min). Kills only processes whose
parent is PID 1 (orphaned), so active workers' Chrome children — whose ppid
is the runner's Python PID — are NEVER touched.
"""
import gc
import logging
import os
import signal as _signal
import subprocess
import threading

from . import config

logger = logging.getLogger("review")

# Global shutdown event so the daemon exits cleanly on Ctrl+C
shutdown_event = threading.Event()


def _reap_orphans(comm_substrings):
    """Kill processes with ppid==1 whose comm contains any substring."""
    killed = 0
    try:
        ps = subprocess.run(
            ["/bin/ps", "-eo", "ppid,pid,comm"],
            capture_output=True, text=True,
        )
        for line in ps.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            ppid, pid, comm = parts[0], parts[1], parts[2]
            if ppid != "1":
                continue
            if any(sub in comm for sub in comm_substrings):
                try:
                    os.kill(int(pid), _signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception as e:
        logger.warning(f"[Cleanup] reap failed: {e}")
    return killed


def _drop_caches():
    try:
        subprocess.run(["/bin/sync"], capture_output=True)
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("1\n")
        return True
    except Exception:
        return False


def _cleanup_loop():
    while not shutdown_event.wait(timeout=config.MEM_CLEANUP_INTERVAL):
        try:
            collected = gc.collect()
            dropped = _drop_caches()
            killed_chrome = _reap_orphans(("chrome", "chromedriver"))
            killed_disp = _reap_orphans(("Xvfb", "mouseinfo", "sbvirtualdisp"))
            logger.info(
                f"[Cleanup] gc={collected} drop_caches={'OK' if dropped else 'NO'} "
                f"chrome_orphans={killed_chrome} display_orphans={killed_disp}"
            )
        except Exception as e:
            logger.warning(f"[Cleanup] loop error: {e}")


def start():
    """Spawn the daemon thread (idempotent)."""
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup-daemon")
    t.start()
    logger.info(f"[Cleanup] daemon started — interval={config.MEM_CLEANUP_INTERVAL}s")
    return t


def reap_now():
    """One-shot reap (called on startup to clear orphans from a prior crash)."""
    killed_chrome = _reap_orphans(("chrome", "chromedriver"))
    killed_disp = _reap_orphans(("Xvfb", "mouseinfo", "sbvirtualdisp"))
    if killed_chrome or killed_disp:
        logger.info(
            f"[Cleanup] startup reap: chrome={killed_chrome} display={killed_disp}"
        )


def stop():
    shutdown_event.set()
