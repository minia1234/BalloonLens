"""Regression tests for parallel CLI process ownership and cancellation-before-dispatch.

These tests use fake processes/backends: they neither log in nor spend subscription quota.
"""
import threading
import time

from balloonlens.backends import base as base_module
from balloonlens.backends.base import BackendError, ProcessRunner
from balloonlens.backends.page import CliTranslator
from balloonlens.cache import Cache


def _wait_until(predicate, timeout=2.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for concurrent calls")


class _WaitingProcess:
    def __init__(self):
        self.returncode = None
        self._done = threading.Event()
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("fake process should have been cancelled")
        return b"", b""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def test_runner_cancel_reaches_all_inflight_children(monkeypatch):
    children = []
    lock = threading.Lock()

    def popen(*args, **kwargs):
        child = _WaitingProcess()
        with lock:
            children.append(child)
        return child

    # Avoid an OS-specific taskkill branch: exercise the ProcessRunner ownership contract.
    monkeypatch.setattr(base_module, "kill_tree", lambda child: child.kill())
    runner = ProcessRunner(popen=popen)
    errors = []

    def run():
        try:
            runner.run(["fake-cli"], stdin=b"data", cwd=".", env={}, timeout=3)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()

    def three_active():
        with runner._lock:
            return len(runner._active) == 3

    _wait_until(three_active)
    runner.cancel()
    for thread in threads:
        thread.join(1)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(children) == 3
    assert all(child.killed for child in children)
    with runner._lock:
        assert not runner._active


class _ImmediateBackend:
    backend_id = "claude"
    cache_id = "claude-cli"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        self.calls += 1
        return {"data": {"items": []}, "usage": {"input": 0, "output": 0, "cached": 0}, "seconds": 0}


def test_cancel_while_waiting_for_call_start_gap_skips_cli(tmp_path):
    backend = _ImmediateBackend()
    translator = CliTranslator(backend, Cache(tmp_path / "cache.sqlite3"), parallel=1)
    translator.min_gap = 0.35
    translator.call_json("prompt", b"{}", model="claude-haiku-5-5")

    cancel = threading.Event()
    errors = []

    def second_call():
        try:
            translator.call_json("prompt", b"{}", model="claude-haiku-5-5",
                                 should_cancel=cancel.is_set)
        except BackendError as exc:
            errors.append(exc.code)

    worker = threading.Thread(target=second_call, daemon=True)
    worker.start()
    _wait_until(lambda: translator._inflight == 1)
    cancel.set()
    worker.join(2)
    assert not worker.is_alive()
    assert errors == ["cancelled"]
    assert backend.calls == 1  # the second request was reserved, but never sent
    assert translator._inflight == 0
