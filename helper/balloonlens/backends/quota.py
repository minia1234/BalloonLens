"""Local diagnostics and pauses after actual subscription limit responses. No app-imposed call caps."""
import time

from .base import BackendError

WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 86400
DEFAULT_PAUSE_S = 1800
MIN_PAUSE_S = 60
MAX_PAUSE_S = 2 * 3600
STREAK_LOOKUP = 8  # recent ledger rows scanned for repeated limit hits


def backoff_s(streak: int) -> int:
    """30 minutes after the first limit hit, doubling for each repeat, up to 2 hours (plan §9.4)."""
    return min(DEFAULT_PAUSE_S * 2 ** max(streak - 1, 0), MAX_PAUSE_S)


def _midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class QuotaPolicy:
    def __init__(self, cache, clock=time.time):
        self.cache = cache
        self.clock = clock

    # ----- ledger -----
    def usage(self, name: str) -> dict:
        now = self.clock()
        return self.cache.usage_summary(name, since_7d=now - WINDOW_7D, since_5h=now - WINDOW_5H,
                                        since_today=_midnight(now))

    def record(self, *, backend: str, model: str, lines: int, usage, seconds, outcome: str, retry_after=None):
        self.cache.record_usage(self.clock(), backend, model, lines, usage or {}, seconds, outcome)
        if outcome == "quota_exhausted":
            self.pause(backend, retry_after or backoff_s(self._limit_streak(backend)), "구독 한도에 도달했어요")

    def _limit_streak(self, name: str) -> int:
        """Limit hits in a row, newest first. The row just recorded counts as the first."""
        streak = 0
        for outcome in self.cache.recent_outcomes(name, STREAK_LOOKUP):
            if outcome != "quota_exhausted":
                break
            streak += 1
        return streak

    # ----- pauses -----
    def pause_info(self, name: str):
        pause = self.cache.get_pause(name)
        now = self.clock()
        if not pause or pause["until"] <= now:
            return None
        return {"until": pause["until"], "reason": pause["reason"], "retry_in_s": round(pause["until"] - now)}

    def pause(self, name: str, seconds, reason: str):
        seconds = min(max(float(seconds or DEFAULT_PAUSE_S), MIN_PAUSE_S), MAX_PAUSE_S)
        self.cache.put_pause(name, self.clock() + seconds, reason)

    def resume(self, name: str):
        self.cache.clear_pause(name)

    # ----- gate -----
    def check(self, name: str, pending: int = 0):
        """Only actual Claude limit responses pause calls. Legacy stored caps and pending counts do not block."""
        pause = self.pause_info(name)
        if pause:
            raise BackendError("quota_paused", pause["reason"], retry_after=pause["retry_in_s"])
