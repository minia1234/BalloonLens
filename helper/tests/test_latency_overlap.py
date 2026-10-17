"""Independent preparation should overlap the OCR and translation waits, without extra model calls."""
import io
import threading

import pytest
from PIL import Image

from balloonlens import jobs
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.prompts import system_prompt
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.backends.router import Router
from balloonlens.cache import Cache
from balloonlens.jobs import JobManager
from balloonlens.ocr import decode_image
from ocr_fakes import PageOcr
from test_backends import FakeBackend

MODEL = "claude-haiku-5-5"


class PreparedBackend(FakeBackend):
    def __init__(self, **kwargs):
        super().__init__("claude", **kwargs)
        self.prepared = []

    def prepare(self, system, *, model=None):
        self.prepared.append((system, model))


class OneBalloon(PageOcr):
    def __init__(self, on_read=lambda: None):
        self.on_read = on_read

    def run(self, img, src, reading):
        self.on_read()
        return [{"id": 0, "bbox": [10, 10, 40, 30], "src": "待って", "conf": 1.0}], {}


def run_job(cache, backend, ocr, options=None):
    quota = QuotaPolicy(cache)
    translator = CliTranslator(backend, cache, quota)
    router = Router(local=None, cli={"claude": translator}, backends={"claude": backend}, quota=quota, cache=cache)
    manager = JobManager(ocr, None, cache, decode_image, router=router)
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buf, "PNG")
    job = manager.submit(buf.getvalue(), src="ja", tgt="ko", model=MODEL, reading="rtl", priority=1,
                         session="test", scope="", options=options or {})
    manager.process(job)
    return job


def test_claude_prepares_during_ocr_with_the_actual_work_instructions(tmp_path):
    backend = PreparedBackend()
    seen = []
    ocr = OneBalloon(lambda: seen.append((list(backend.prepared), len(backend.calls))))
    job = run_job(Cache(tmp_path / "c.db"), backend, ocr, {"instructions": "친근한 말투로 번역"})
    assert job.status == "done", job.error
    assert seen == [([(system_prompt("ja", "ko", "친근한 말투로 번역"), MODEL)], 0)]
    assert len(backend.calls) == 1
    assert backend.calls[0]["system"] == backend.prepared[0][0]


@pytest.mark.parametrize("state", ["not_logged_in", "ready"])
def test_a_blocked_job_does_not_prepare_or_call_claude(tmp_path, state):
    backend = PreparedBackend(state=state)
    cache = Cache(tmp_path / "c.db")
    if state == "ready":
        QuotaPolicy(cache).pause("claude", 3600, "quota_exhausted")
    job = run_job(cache, backend, OneBalloon())
    assert job.status == "failed"
    assert backend.prepared == [] and backend.calls == []


def test_presentation_is_prepared_while_claude_is_answering(tmp_path, monkeypatch):
    translating, presented = threading.Event(), threading.Event()
    overlapped = []
    backend = PreparedBackend()
    reply = backend.handler

    def answer(payload):
        translating.set()
        overlapped.append(presented.wait(1))
        return reply(payload)

    original = jobs.prepare_presentation

    def present(img, segments):
        assert translating.wait(1)
        out = original(img, segments)
        presented.set()
        return out

    backend.handler = answer
    monkeypatch.setattr(jobs, "prepare_presentation", present)
    job = run_job(Cache(tmp_path / "c.db"), backend, OneBalloon())
    assert job.status == "done", job.error
    assert overlapped == [True]
    assert job.segments[0]["tgt"] == "번역문"
    assert "presentation" in job.segments[0]
