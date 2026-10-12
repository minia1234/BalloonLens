"""Cancelling a page stops its own CLI work: no call starts after the cancel, a running process is stopped, and a failed
job reports a code, never a message. Fake processes and backends only; nothing logs in or spends quota."""
import io
import threading
import time

import pytest
from PIL import Image

from balloonlens.backends import base as base_module
from balloonlens.backends.base import BackendError, CallCancelled, ProcessRunner, cancel_scope
from balloonlens.backends.page import CliTranslator
from balloonlens.cache import Cache
from balloonlens.jobs import FINISHED, JobManager, error_code
from balloonlens.ocr import decode_image

CLAUDE = "claude-haiku-5-5"


def wait_until(predicate, timeout=3.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


class Counting:
    backend_id = "claude"
    cache_id = "claude-cli"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        self.calls += 1
        return {"data": {"items": []}, "usage": {"input": 0, "output": 0, "cached": 0}, "seconds": 0}


def test_a_cancelled_page_never_starts_a_call(tmp_path):
    backend = Counting()
    translator = CliTranslator(backend, Cache(tmp_path / "c.db"))
    with pytest.raises(CallCancelled):
        translator.call_json("prompt", b"{}", model=CLAUDE, should_cancel=lambda: True)
    assert backend.calls == 0


def test_cancel_during_the_start_gap_skips_the_call(tmp_path):
    backend = Counting()
    translator = CliTranslator(backend, Cache(tmp_path / "c.db"), parallel=1)
    translator.min_gap = 0.35
    translator.call_json("prompt", b"{}", model=CLAUDE)  # the first call starts now and reserves the next start

    cancel = threading.Event()
    outcome = []

    def second():
        try:
            translator.call_json("prompt", b"{}", model=CLAUDE, should_cancel=cancel.is_set)
            outcome.append("ran")
        except CallCancelled:
            outcome.append("cancelled")

    worker = threading.Thread(target=second, daemon=True)
    worker.start()
    wait_until(lambda: translator._inflight == 1)  # the second call is waiting for its start time
    cancel.set()
    worker.join(2)
    assert outcome == ["cancelled"]
    assert backend.calls == 1  # the reserved start was not used


class FakeChild:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self._done = threading.Event()

    def communicate(self, input=None, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("child should have been stopped")
        return b"", b""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def test_cancelling_one_page_stops_only_its_own_process(monkeypatch):
    children = []
    monkeypatch.setattr(base_module, "kill_tree", lambda child: child.kill())
    runner = ProcessRunner(popen=lambda *a, **k: children.append(FakeChild()) or children[-1])
    cancel_mine = threading.Event()

    def run(scope):
        with cancel_scope(scope):
            runner.run(["fake-cli"], stdin=b"", cwd=".", env={}, timeout=5)

    mine = threading.Thread(target=run, args=(cancel_mine.is_set,), daemon=True)
    other = threading.Thread(target=run, args=(lambda: False,), daemon=True)
    mine.start()
    other.start()
    wait_until(lambda: len(children) == 2)
    cancel_mine.set()
    mine.join(2)
    wait_until(lambda: any(child.killed for child in children))
    assert sum(child.killed for child in children) == 1  # only the cancelled page's process
    survivor = next(child for child in children if not child.killed)
    runner.cancel()  # cleanup for the test only
    other.join(2)
    assert survivor.killed


def test_a_failed_job_reports_a_code_not_the_message():
    assert error_code(ValueError("원문 대사 secret")) == "ValueError"
    assert error_code(BackendError("quota_paused", "원문 대사 secret")) == "quota_paused"


def png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return buf.getvalue()


class ExplodingOcr:
    def run(self, img, src, reading):
        raise ValueError("말풍선 원문: 絶対に言わないで")


class Unused:
    def translate_page(self, *args, **kwargs):
        raise AssertionError("the job must fail before translation")


def test_the_job_panel_and_metrics_see_only_the_code(tmp_path):
    manager = JobManager(ExplodingOcr(), Unused(), Cache(tmp_path / "c.db"), decode_image,
                         metrics_path=tmp_path / "metrics.jsonl")
    manager.start()
    job = manager.submit(png(), src="ja", tgt="ko", model="qwen3.5:9b", reading="rtl", priority=2,
                         session="s", scope="x")
    wait_until(lambda: manager.get(job.id)["status"] in FINISHED)
    snap = manager.get(job.id)
    assert snap["status"] == "failed"
    assert snap["error"] == "ValueError"
    metrics = tmp_path / "metrics.jsonl"
    wait_until(lambda: metrics.exists() and metrics.read_text(encoding="utf-8").strip())  # written after the status
    assert "絶対" not in metrics.read_text(encoding="utf-8")
