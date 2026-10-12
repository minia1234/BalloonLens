"""Local usage ledger, self-imposed caps and pauses for the subscription backends.

The ledger counts calls and tokens. It never stores dialogue text. Official usage percentages are not
read automatically (no public interface was confirmed); the reader can type them in, and the
difference between two readings gives a per-call estimate.
"""
import time

from .base import BackendError

DEFAULT_CAPS = {"claude": {"per_5h": 20, "per_7d": 150}}
WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 86400
DEFAULT_PAUSE_S = 1800
MIN_PAUSE_S = 60
MAX_PAUSE_S = 2 * 3600
MAX_CAP = 10000
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

    # ----- caps -----
    def caps(self, name: str) -> dict:
        stored = self.cache.get_caps(name)
        return dict(stored) if stored else dict(DEFAULT_CAPS[name])

    def set_caps(self, name: str, per_5h: int, per_7d: int):
        if (per_5h, per_7d) == (0, 0):  # both 0: no self-imposed limit; the official usage limit still stops calls
            self.cache.put_caps(name, 0, 0)
            return
        for value in (per_5h, per_7d):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_CAP:
                raise ValueError("bad_caps")
        if per_5h > per_7d:
            raise ValueError("bad_caps")  # a 5-hour cap above the weekly cap can never bind as intended
        self.cache.put_caps(name, per_5h, per_7d)

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
        """Raises BackendError('quota_paused') when a pause or a cap blocks the next call.

        pending counts calls already started but not yet recorded, so parallel calls cannot overshoot a cap."""
        pause = self.pause_info(name)
        if pause:
            raise BackendError("quota_paused", pause["reason"], retry_after=pause["retry_in_s"])
        now = self.clock()
        caps = self.caps(name)
        if caps["per_5h"] == 0 and caps["per_7d"] == 0:  # no self-imposed limit
            return
        for window, limit, label in ((WINDOW_5H, caps["per_5h"], "5시간"), (WINDOW_7D, caps["per_7d"], "7일")):
            if self.cache.count_usage(name, now - window) + pending >= limit:
                oldest = self.cache.oldest_usage(name, now - window)
                retry = (oldest + window - now) if oldest else window
                raise BackendError("quota_paused", f"{label} 호출 상한 {limit}회에 도달", retry_after=max(0.0, retry))

    # ----- calibration with the official percentage -----
    def add_calibration(self, name: str, percent: float):
        if not 0 <= percent <= 100:
            raise ValueError("bad_percent")
        self.cache.add_calibration(name, self.clock(), percent, self.cache.total_usage(name))

    def calibration_estimate(self, name: str):
        rows = self.cache.list_calibration(name, limit=2)
        if len(rows) < 2:
            return None
        newest, older = rows
        calls = newest["calls_total"] - older["calls_total"]
        delta = newest["percent"] - older["percent"]
        if calls <= 0 or delta < 0:
            return None
        return {"per_call_percent": round(delta / calls, 3), "calls_between": calls,
                "percent_between": round(delta, 2)}
