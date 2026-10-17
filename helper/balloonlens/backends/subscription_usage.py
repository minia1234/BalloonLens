"""Claude account usage metadata. Only sanitized percentages/times leave this module.

The /usage service is an internal Claude endpoint, so missing fields and unavailable responses stay unknown.
It is queried independently of translation, at most once per five minutes across all extension views.
"""
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_S = 300


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def parse_windows(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    meters = data.get("limits")
    meters = meters if isinstance(meters, list) else []
    windows = []
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        window = data.get(key)
        if not isinstance(window, dict):
            window = next((m for m in meters if isinstance(m, dict) and m.get("kind") == kind), {})
        used = window.get("utilization", window.get("percent"))
        if type(used) not in (int, float) or not math.isfinite(used):
            continue
        used = min(100, max(0, used))
        windows.append({"id": key, "remaining_percent": round(100 - used, 1),
                        "resets_at": _timestamp(window.get("resets_at"))})
    return windows


class UsageUnavailable(Exception):
    def __init__(self, code, retry_after=CACHE_S):
        self.code, self.retry_after = code, retry_after


class ClaudeUsage:
    def __init__(self, config_dir, *, fetch=None, clock=time.time):
        self.config_dir = Path(config_dir)
        self.clock = clock
        self._fetch = fetch or self._fetch_account
        self._lock = threading.Lock()
        self._windows = []
        self._checked_at = None
        self._next_fetch = 0
        self._refreshing = False
        self._error = None

    def _fetch_account(self):
        try:
            credentials = json.loads((self.config_dir / ".credentials.json").read_text(encoding="utf-8"))
            oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
            token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        except (OSError, ValueError):
            token = None
        if not isinstance(token, str) or not token.strip():
            raise UsageUnavailable("not_logged_in")
        # Never follow a redirect carrying the bearer token to a different host.
        response = httpx.get(USAGE_URL, headers={"Authorization": "Bearer " + token,
                                                "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"},
                             timeout=5, follow_redirects=False)
        if response.status_code != 200:
            retry = response.headers.get("retry-after", "")
            try:
                retry = min(3600, max(CACHE_S, float(retry)))
            except ValueError:
                retry = CACHE_S
            code = "rate_limited" if response.status_code == 429 else "unavailable"
            if response.status_code in (401, 403):
                code = "not_logged_in"
            raise UsageUnavailable(code, retry)
        return response.json()

    def _refresh(self):
        windows, error, delay = [], None, CACHE_S
        try:
            windows = parse_windows(self._fetch())
            if not windows:
                error = "unavailable"
        except UsageUnavailable as exc:
            error, delay = exc.code, exc.retry_after
        except (httpx.HTTPError, OSError, ValueError):
            error = "unavailable"  # exception text may contain request headers; never return or log it
        with self._lock:
            now = self.clock()
            if not error:
                self._windows, self._checked_at = windows, now
                resets = [w["resets_at"] - now for w in windows if w["resets_at"] and w["resets_at"] > now]
                if resets:
                    delay = min(delay, max(1, min(resets)))
            self._error = error
            self._next_fetch = now + delay
            self._refreshing = False

    def snapshot(self):
        with self._lock:
            now = self.clock()
            if not self._refreshing and now >= self._next_fetch:
                self._refreshing = True
                threading.Thread(target=self._refresh, name="claude-usage", daemon=True).start()
            windows = [dict(w) for w in self._windows]
            expired = False
            for window in windows:
                if window["resets_at"] and window["resets_at"] <= now:
                    window["remaining_percent"] = None
                    expired = True
            return {"state": "loading" if self._refreshing else "ready" if windows else "unavailable",
                    "windows": windows, "checked_at": self._checked_at,
                    "stale": bool(windows and (self._error or expired or now - self._checked_at >= CACHE_S)),
                    "error": self._error}
