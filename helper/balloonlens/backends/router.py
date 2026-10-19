"""Picks the translator for a model and decides whether a subscription call may go out right now.

Gate order (cheapest first): pauses after actual Claude limit responses, then the CLI's own login state (cached for a
short time so jobs don't start a CLI process each).
"""
import time

from .base import BackendError, BackendStatus

STATUS_TTL_S = 120  # a ready status is trusted this long
NOT_READY_TTL_S = 5  # anything else (not logged in, not installed...) is checked again soon, so a fresh login counts at once
SUBSCRIPTION_PREFIXES = {"claude": ("claude-",)}


class Router:
    def __init__(self, *, local, cli: dict, backends: dict, quota, cache, clock=time.time):
        self.local = local
        self.cli = cli
        self.backends = backends
        self.quota = quota
        self.cache = cache
        self.clock = clock
        self._status: dict[str, BackendStatus] = {}

    @staticmethod
    def backend_of(model: str):
        for name, prefixes in SUBSCRIPTION_PREFIXES.items():
            if model.startswith(prefixes):
                return name
        return None

    def is_subscription(self, model: str) -> bool:
        return self.backend_of(model) is not None

    def translator_for(self, model: str):
        name = self.backend_of(model)
        return self.cli[name] if name else self.local

    def status(self, name: str, refresh: bool = False) -> BackendStatus:
        cached = self._status.get(name)
        ttl = STATUS_TTL_S if cached is not None and cached.ready else NOT_READY_TTL_S
        if refresh or cached is None or self.clock() - cached.checked_at > ttl:
            try:
                st = self.backends[name].preflight()
            except Exception as e:  # a broken CLI must not take the worker down
                st = BackendStatus(backend=name, state="backend_error", detail=type(e).__name__,
                                   checked_at=self.clock())
            self._status[name] = st
        return self._status[name]

    def gate(self, model: str):
        """None when a call may go out, else the error code to show the reader."""
        name = self.backend_of(model)
        if name is None:
            return None
        try:
            self.quota.check(name)
        except BackendError as e:
            return e.code
        st = self.status(name)
        return None if st.ready else st.state

    def summary(self) -> list[dict]:
        out = []
        for name, backend in self.backends.items():
            st = self._status.get(name) or BackendStatus(backend=name)
            usage_status = getattr(backend, "usage_status", None)
            out.append({
                "id": name,
                "label": backend.label,
                "model": backend.model,
                "state": st.state,
                "detail": st.detail,
                "version": st.version,
                "checked_at": st.checked_at,
                "paused": self.quota.pause_info(name),
                "subscription_usage": usage_status() if usage_status else None,
                "login_command": backend.login_command() if hasattr(backend, "login_command") else None,
            })
        return out
