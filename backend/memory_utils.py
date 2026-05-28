"""
memory_utils.py — utility per il contenimento della memoria (fix OOM Render).

L'app gira su Render Starter (512MB). Lo stack (pandas, yfinance, anthropic,
fastapi) e' pesante e Python/glibc tende a NON restituire subito la RAM
liberata all'OS. Questo modulo offre:
  - get_rss_mb(): RSS corrente del processo (per monitor/health check)
  - trim_memory(): gc.collect() + malloc_trim(0) → restituisce RAM all'OS

Pensato per essere chiamato dopo i job pesanti (pipeline decisionale,
simulator) e periodicamente dallo scheduler. Tutto best-effort: non lancia
mai eccezioni che possano rompere il chiamante.
"""
import gc
import logging
import os
import sys

logger = logging.getLogger(__name__)


def get_rss_mb() -> float | None:
    """RSS corrente del processo in MB. None se non determinabile."""
    # Linux (Render): /proc/self/status VmRSS = RSS CORRENTE (non il picco)
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)  # KB → MB
    except Exception:
        pass
    # Fallback (macOS/altro): ru_maxrss (picco, non corrente)
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS in byte, Linux in KB
        return round(rss / (1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0), 1)
    except Exception:
        return None


def trim_memory(reason: str = "") -> dict:
    """Forza gc + restituisce la RAM libera all'OS (glibc malloc_trim).

    malloc_trim(0) e' glibc-only (Linux/Render): rilascia gli arena chunk
    liberi. Su Windows/macOS la chiamata viene saltata silenziosamente
    (il gc.collect() avviene comunque).
    """
    rss_before = get_rss_mb()
    collected = 0
    try:
        collected = gc.collect()
    except Exception:
        pass
    trimmed = False
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
            trimmed = True
    except Exception:
        # Non-glibc (Windows/macOS) o libc non caricabile: ok, skip.
        pass
    rss_after = get_rss_mb()
    result = {
        "reason": reason,
        "gc_collected": collected,
        "malloc_trimmed": trimmed,
        "rss_mb_before": rss_before,
        "rss_mb_after": rss_after,
    }
    if rss_before and rss_after and (rss_before - rss_after) > 5:
        logger.info("[MEM] trim%s: %.1f → %.1f MB (gc=%d, trim=%s)",
                    f" ({reason})" if reason else "",
                    rss_before, rss_after, collected, trimmed)
    return result
