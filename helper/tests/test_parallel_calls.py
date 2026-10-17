"""Subscription calls run side by side: the chunks of one page, and the jobs of several images (CLI_PARALLEL).

The fake CLI holds each call for a moment, so overlapping calls show up as a peak above one."""
import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_fakes import PageOcr
from balloonlens import config
from balloonlens.backends.base import BackendStatus, write_once
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.backends.router import Router
from balloonlens.cache import Cache
from balloonlens.jobs import JobManager
from balloonlens.ocr import decode_image
from balloonlens.server import create_app

TOKEN = "t0ken"
EXT = "ourextensionid"
AUTH = {"host": "127.0.0.1:8787", "x-balloonlens-token": TOKEN}
CLAUDE = "claude-haiku-5-5"
FINISHED = ("done", "partial", "failed", "cancelled")


def noop(*args):
    return None


class Busy:
    """Holds every call for a moment and records how many calls overlap and what each one was asked."""

    backend_id = "claude"
    cache_id = "claude-cli"
    label = "Claude"
    model = CLAUDE

    def __init__(self, hold=0.3, together=0):
        self.hold = hold
        self.active = 0
        self.peak = 0
        self.payloads = []
        self._lock = threading.Lock()
        # With `together`, each call waits until that many calls are in flight at once. A sequential run breaks the
        # barrier instead, so the test fails instead of depending on timing.
        self._barrier = threading.Barrier(together, timeout=10) if together else None

    def preflight(self):
        return BackendStatus("claude", "ready", "fake", "", time.time())

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        body = json.loads(payload)
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.payloads.append(body)
        try:
            if self._barrier is not None:
                self._barrier.wait()
            time.sleep(self.hold)
        finally:
            with self._lock:
                self.active -= 1
        items = [{"id": sid, "text": "번역문", "review": False} for sid in body["target_ids"]]
        return {"data": {"items": items}, "usage": {"input": 1, "output": 1, "cached": 0}, "seconds": self.hold}


def digit_free_lines(count):
    return [(i + 1, "台詞" + "あ" * (i % 5 + 1)) for i in range(count)]


def test_chunks_of_one_page_run_side_by_side_and_each_sees_the_whole_page(tmp_path):
    backend = Busy(together=3)
    got = {}
    meta = CliTranslator(backend, Cache(tmp_path / "c.db"), parallel=3).translate_page(
        digit_free_lines(60), "ja", "ko", CLAUDE, "scope", lambda sid, text, status: got.__setitem__(sid, status))
    assert backend.peak == 3  # 60 lines = 24 + 24 + 12, all three in flight together
    assert len(got) == 60 and set(got.values()) == {"done"}
    assert meta["calls"] == 3
    assert all(len(p["page"]) == 60 for p in backend.payloads)
    assert all(p["context"] == [] for p in backend.payloads)  # parallel chunks do not wait for each other's replies


def test_parallel_calls_continue_despite_old_stored_caps(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    cache.put_caps("claude", 2, 10)
    backend = Busy(hold=0.6)  # long enough that the first two calls are still in flight when the third one checks
    meta = CliTranslator(backend, cache, quota, parallel=3).translate_page(
        digit_free_lines(60), "ja", "ko", CLAUDE, "scope", noop)
    assert len(backend.payloads) == 3
    assert not meta.get("fatal")
    assert quota.usage("claude")["calls_5h"] == 3


def test_write_once_keeps_the_first_text_under_a_race(tmp_path):
    target = tmp_path / "system-claude-abc.txt"
    threads = [threading.Thread(target=write_once, args=(target, "same text")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert target.read_text(encoding="utf-8") == "same text"
    assert not list(tmp_path.glob("*.tmp"))


def png_with(k):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (k * 40, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


class PerImageOcr(PageOcr):
    """Two lines per image; the text depends on the image, so no two images share a translation-cache entry."""

    def run(self, img, src, reading):
        k = img.getpixel((0, 0))[0] // 40
        return [{"id": 0, "bbox": [1, 2, 3, 4], "src": "台" + chr(0x3041 + k), "conf": None},
                {"id": 1, "bbox": [5, 6, 7, 8], "src": "詞" + chr(0x3041 + k), "conf": None}], {"detect_ms": 1}


class NoLocal:
    def translate_page(self, *args, **kwargs):
        raise AssertionError("the local translator must not run for a subscription model")


@pytest.fixture
def helper(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    backend = Busy(hold=0.2, together=config.CLI_PARALLEL)
    router = Router(local=NoLocal(), cli={"claude": CliTranslator(backend, cache, quota)},
                    backends={"claude": backend}, quota=quota, cache=cache)
    manager = JobManager(PerImageOcr(), NoLocal(), cache, decode_image, router=router)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"], extension_ids={EXT}))
    return client, backend


def wait_job(client, job_id):
    for _ in range(300):
        snap = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if snap["status"] in FINISHED:
            return snap
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_a_second_image_is_read_while_the_first_is_still_translating(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLI_PARALLEL", 1)  # one call at a time: the first image holds the only call slot
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    backend = Busy(hold=1.0)
    translator = CliTranslator(backend, cache, quota, parallel=1)  # one call at a time, so the first one blocks
    router = Router(local=NoLocal(), cli={"claude": translator}, backends={"claude": backend}, quota=quota, cache=cache)
    manager = JobManager(PerImageOcr(), NoLocal(), cache, decode_image, router=router)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"], extension_ids={EXT}))
    first =client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(0), headers=AUTH).json()["id"]
    for _ in range(200):
        if backend.active == 1:
            break
        time.sleep(0.01)
    second = client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(1), headers=AUTH).json()["id"]
    # The second image starts reading at once; it does not queue behind the first image's translation call.
    assert wait_job(client, second)["timings"]["queue_ms"] < 500
    assert wait_job(client, first)["status"] == "done"


def test_subscription_jobs_for_several_images_run_at_once(helper):
    client, backend = helper
    # One image per allowed slot: the barrier in the fake CLI only opens when all of them are in flight together.
    ids = [client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(k), headers=AUTH).json()["id"]
           for k in range(config.CLI_PARALLEL)]
    for job_id in ids:
        assert wait_job(client, job_id)["status"] == "done"
    assert backend.peak == config.CLI_PARALLEL


def test_a_page_already_read_does_not_wait_for_the_reading_lock(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    backend = Busy(hold=0.01)
    router = Router(local=NoLocal(), cli={"claude": CliTranslator(backend, cache, quota)},
                    backends={"claude": backend}, quota=quota, cache=cache)
    manager = JobManager(PerImageOcr(), NoLocal(), cache, decode_image, router=router)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"], extension_ids={EXT}))
    first = client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(0), headers=AUTH).json()["id"]
    assert wait_job(client, first)["status"] == "done"
    # Another page holds the reading lock for a while. A page that is already in the OCR cache needs no reading,
    # so it must finish while the lock is still held (wait_job gives up after 15 s if it waits for the lock).
    manager._ocr_lock.acquire()
    try:
        again = client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(0), headers=AUTH).json()["id"]
        snap = wait_job(client, again)
    finally:
        manager._ocr_lock.release()
    assert snap["status"] == "done"
    assert snap["timings"]["ocr_cache"] is True


class CountingOcr(PerImageOcr):
    """Counts the reads. The sleep keeps the first read busy while a second job for the same image arrives."""

    def __init__(self):
        self.reads = 0

    def run(self, img, src, reading):
        self.reads += 1
        time.sleep(0.3)
        return super().run(img, src, reading)


def test_two_jobs_for_one_image_read_it_once(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    backend = Busy(hold=0.01)
    router = Router(local=NoLocal(), cli={"claude": CliTranslator(backend, cache, quota)},
                    backends={"claude": backend}, quota=quota, cache=cache)
    ocr = CountingOcr()
    manager = JobManager(ocr, NoLocal(), cache, decode_image, router=router)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"], extension_ids={EXT}))
    ids = [client.post(f"/v1/jobs?src=ja&tgt=ko&model={CLAUDE}", content=png_with(0), headers=AUTH).json()["id"]
           for _ in range(2)]
    for job_id in ids:
        assert wait_job(client, job_id)["status"] == "done"
    assert ocr.reads == 1  # the second job waited for the lock, then found the page already read
