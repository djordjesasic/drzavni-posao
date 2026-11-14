"""Daily pause window for mass processing (the shared services' own nightly job)."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Belgrade")


def _parse(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


def seconds_until_resume(now: datetime, start: str, end: str) -> float:
    """0 when outside [start, end) local time, else seconds until end."""
    local = now.astimezone(TZ)
    s, e = _parse(start), _parse(end)
    t = local.time()
    paused = s <= t < e if s <= e else (t >= s or t < e)
    if not paused:
        return 0.0
    resume = local.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if resume <= local:
        resume += timedelta(days=1)
    return (resume - local).total_seconds()


def wait_if_paused(
    start: str,
    end: str,
    now: Callable[[], datetime] = lambda: datetime.now(TZ),
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Block until the pause window is over; return how long we waited."""
    wait = seconds_until_resume(now(), start, end)
    if wait > 0:
        log.info("pause window %s–%s: sleeping %.0f s", start, end, wait)
        sleep(wait)
    return wait
