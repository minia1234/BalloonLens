import io
import threading
import time

import pytest
from PIL import Image

from balloonlens.cache import Cache
from balloonlens import jobs
from balloonlens.jobs import FINISHED, JobManager
from balloonlens.ocr import PlanStep, decode_image, visible_first
from ocr_fakes import PageOcr


def png(color=255):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (color, color, color)).save(buf, "PNG")
    return buf.getvalue()


def balloons(count):
    return [{"id": i, "bbox": [1, 2 + 10 * i, 3, 4], "src": f"大{i}", "conf": None} for i in range(count)]


class PagesOcr(PageOcr):
    """A page of `count` balloons. Each read_spans() call is recorded, and the second one sets `second_read`."""

    def __init__(self, count):
        self.count = count
        self.reads = []
        self.second_read = threading.Event()

    def run(self, img, src, reading):
        return balloons(self.count), {}

    def read_spans(self, img, spans, src, stats=None):
        self.reads.append(len(spans))
        if len(self.reads) == 2:
            self.second_read.set()
        return super().read_spans(img, spans, src, stats)


class GatedTranslator:
    """The first step waits until the second balloon chunk is being read. That can only happen if reading and translating overlap."""

    def __init__(self, ocr, wait_s=10):
        self.ocr = ocr
        self.wait_s = wait_s
        self.pages = []
        self.gate_passed = None

    def translate_page(self, lines, src, tgt, model, scope, on_result, **options):
        self.pages.append(len(lines))
        if len(self.pages) == 1:
            self.gate_passed = self.ocr.second_read.wait(self.wait_s)
        fixed = options.get("fixed") or {}
        for sid, text in lines:
            if sid not in fixed:
                on_result(sid, f"번역:{text}", "done")
        return {"load_ms": 0}


def run_page(tmp_path, ocr, translator, options=None):
    cache = Cache(tmp_path / "c.db")
    manager = JobManager(ocr, translator, cache, decode_image)
    manager.start()
    job = manager.submit(png(), src="ja", tgt="ko", model="qwen3.5:9b", reading="rtl", priority=2, session="s",
                         scope="", options=options or {})
    deadline = time.time() + 20
    while job.status not in FINISHED and time.time() < deadline:
        time.sleep(0.02)
    return job


def test_a_long_page_is_translated_while_its_later_balloons_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PIPELINE_REST_S", 0)  # whatever the reading speed: the page is split
    ocr = PagesOcr(count=24)
    translator = GatedTranslator(ocr)
    job = run_page(tmp_path, ocr, translator)
    assert job.status == "done", job.error
    assert translator.gate_passed is True  # the second chunk was read while the first one was translated
    assert ocr.reads == [8, 8, 8]
    assert translator.pages == [8, 24]  # two calls: the first 8, then the other 16 with the 8 translated ones as reference
    assert [s["tgt"] for s in job.segments] == [f"번역:大{i}" for i in range(24)]
    assert job.timings["steps"] == 2


def test_a_short_page_is_one_translation_call_as_before(tmp_path):
    ocr = PagesOcr(count=3)
    translator = GatedTranslator(ocr, wait_s=0)
    job = run_page(tmp_path, ocr, translator)
    assert job.status == "done", job.error
    assert translator.pages == [3]
    assert ocr.reads == [3]


class SlowPagesOcr(PagesOcr):
    """Reads 0.02 s per balloon, so the time the rest of the page will take can be told from the first balloons."""

    def read_spans(self, img, spans, src, stats=None):
        time.sleep(0.02 * len(spans))
        return super().read_spans(img, spans, src, stats)


def test_the_early_step_goes_out_only_when_reading_the_rest_takes_longer_than_a_call(tmp_path, monkeypatch):
    # Every translator call costs about the same whatever its size, so a page that is read in a moment goes in one call.
    monkeypatch.setattr(jobs, "PIPELINE_REST_S", 0.4)
    expected = {12: ([8, 4], [12]), 16: ([8, 8], [16]), 30: ([8, 8, 8, 6], [8, 30])}
    for count, (reads, pages) in expected.items():
        ocr = SlowPagesOcr(count=count)
        translator = GatedTranslator(ocr, wait_s=0)
        (tmp_path / str(count)).mkdir()
        job = run_page(tmp_path / str(count), ocr, translator)
        assert job.status == "done", job.error
        assert (ocr.reads, translator.pages) == (reads, pages), count
        assert [s["tgt"] for s in job.segments] == [f"번역:大{i}" for i in range(count)]


def test_a_page_that_is_read_in_a_moment_is_never_split(tmp_path):
    ocr = PagesOcr(count=40)  # the default limit: 2.5 s of reading ahead
    translator = GatedTranslator(ocr, wait_s=0)
    job = run_page(tmp_path, ocr, translator)
    assert job.status == "done" and translator.pages == [40]


def test_a_cached_page_is_one_translation_call_however_long_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PIPELINE_REST_S", 0)
    ocr = PagesOcr(count=30)
    translator = GatedTranslator(ocr, wait_s=0)
    cache_dir = tmp_path / "shared"
    cache_dir.mkdir()
    first = run_page(cache_dir, ocr, translator)
    assert first.status == "done" and translator.pages == [8, 30]
    translator.pages.clear()
    ocr.reads.clear()
    second = run_page(cache_dir, ocr, translator)  # the same page again, from the OCR cache
    assert second.status == "done" and second.timings["ocr_cache"] is True
    assert translator.pages == [30] and ocr.reads == []


def test_the_band_the_reader_sees_is_read_first():
    # Three boxes top to bottom in reading order; the reader sees the lower two.
    boxes = [[0, 0, 10, 10], [0, 40, 10, 50], [0, 80, 10, 90]]
    assert visible_first([0, 1, 2], boxes, (35, 95)) == [1, 2, 0]
    assert visible_first([0, 1, 2], boxes, None) == [0, 1, 2]
    assert visible_first([0, 1, 2], boxes, (200, 300)) == [0, 1, 2]  # no box in the band: reading order stays


# ---------- a page planned in several steps ----------

class EchoTranslator:
    def __init__(self):
        self.pages = []

    def translate_page(self, lines, src, tgt, model, scope, on_result, **options):
        self.pages.append(len(lines))
        fixed = options.get("fixed") or {}
        for sid, text in lines:
            if sid not in fixed:
                on_result(sid, f"번역:{text}", "done")
        return {"load_ms": 0}


class StreamedOcr:
    """A page of `count` balloons whose planning is given in steps: `early` are the sizes of the steps before the last one, and the
    last one carries the rest. Records what happened, in order."""

    def __init__(self, count, early=(), on_read=None):
        self.spans = [([None], [1, 2 + 10 * i, 4, 6 + 10 * i]) for i in range(count)]
        self.early = list(early)
        self.events, self.reads, self.visible_seen = [], [], None
        self.on_read = on_read

    def plan_steps(self, img, src, reading, visible=None):
        self.visible_seen = visible
        start = 0
        for size in self.early:
            self.events.append(("step", size))
            yield PlanStep(self.spans[start:start + size])
            start += size
        self.events.append(("step", len(self.spans) - start))
        stats = {"boxes": len(self.spans), "groups": len(self.spans), "tiles": 3, "detect_ms": 5}
        yield PlanStep(self.spans[start:], True, self.spans, list(range(len(self.spans))), stats)

    def read_spans(self, img, spans, src, stats=None):
        self.reads.append([tuple(box) for _, box in spans])
        self.events.append(("read", len(spans)))
        if self.on_read:
            self.on_read(len(self.reads))
        return [(box, f"大{(box[1] - 2) // 10}", None, [], 0) for _, box in spans]


def start_manager(tmp_path, ocr, translator):
    manager = JobManager(ocr, translator, Cache(tmp_path / "c.db"), decode_image)
    manager.start()
    return manager


def submit_and_wait(manager, options=None):
    job = manager.submit(png(), src="ja", tgt="ko", model="qwen3.5:9b", reading="rtl", priority=2, session="s", scope="",
                         options=options or {})
    deadline = time.time() + 20
    while job.status not in FINISHED and time.time() < deadline:
        time.sleep(0.02)
    return job


def test_balloons_found_in_early_steps_are_read_eight_at_a_time_as_soon_as_eight_are_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PIPELINE_REST_S", 0)
    ocr, translator = StreamedOcr(20, early=[3, 3, 6]), EchoTranslator()
    job = submit_and_wait(start_manager(tmp_path, ocr, translator))
    assert job.status == "done", job.error
    # 3 + 3 + 6 balloons are known after the third step: the first eight are read then, before the page is fully planned.
    assert ocr.events == [("step", 3), ("step", 3), ("step", 6), ("read", 8), ("step", 8), ("read", 8), ("read", 4)]
    # Two translator calls for 20 balloons, never one per step: the first 8, and the other 12 with those 8 as reference.
    assert translator.pages == [8, 20]
    assert [s["src"] for s in job.segments] == [f"大{i}" for i in range(20)]
    assert all(s["tgt"] == f"번역:{s['src']}" and s["status"] == "done" for s in job.segments)
    assert job.timings["tiles"] == 3 and job.timings["detect_ms"] == 5 and job.timings["steps"] == 2


def test_a_page_with_few_balloons_is_one_read_and_one_translator_call_however_it_is_planned(tmp_path):
    ocr, translator = StreamedOcr(5, early=[2, 2]), EchoTranslator()
    job = submit_and_wait(start_manager(tmp_path, ocr, translator))
    assert job.status == "done", job.error
    assert ocr.events == [("step", 2), ("step", 2), ("step", 1), ("read", 5)]
    assert translator.pages == [5]


def test_the_stage_times_of_a_page_are_recorded(tmp_path):
    ocr, translator = StreamedOcr(10, early=[10]), EchoTranslator()
    job = submit_and_wait(start_manager(tmp_path, ocr, translator), options={"visible": [0, 0.3]})
    t = job.timings
    assert job.status == "done", job.error
    for key in ("queue_ms", "decode_ms", "memory_ms", "ocr_lock_wait_ms", "detect_ms", "recognize_ms", "ocr_block_ms", "ocr_done_ms",
                "first_read_ms", "first_step_ms", "first_result_ms", "translate_wait_ms", "layout_ms", "present_ms",
                "translate_rest_ms", "translate_ms", "total_ms", "ocr_retries", "ocr_retry_spans", "ocr_retry_fixed",
                "ocr_retry_ms", "ocr_resumed", "visible_read_ms", "boxes", "groups", "tiles", "steps"):
        assert key in t, key
    # The page is read before it is done, and the first text appears before the end of the job.
    assert t["first_read_ms"] <= t["ocr_done_ms"] <= t["total_ms"]
    assert t["first_result_ms"] <= t["total_ms"]
    assert ocr.visible_seen == (0.0, pytest.approx(0.3 * 64))


def test_a_cancelled_page_keeps_what_it_read_and_the_next_job_reads_only_the_rest(tmp_path):
    holder = {}

    def cancel_during_second_read(n):
        if n == 2:
            holder["manager"].cancel_session("s")

    ocr, translator = StreamedOcr(20, on_read=cancel_during_second_read), EchoTranslator()
    manager = holder["manager"] = start_manager(tmp_path, ocr, translator)
    first = submit_and_wait(manager)
    assert first.status == "cancelled"
    assert [len(r) for r in ocr.reads] == [8, 8]  # the chunk that was being read when the job was cancelled is kept
    ocr.reads.clear()
    ocr.on_read = None
    second = submit_and_wait(manager)
    assert second.status == "done", second.error
    assert [len(r) for r in ocr.reads] == [4]  # 16 of the 20 balloons were read already
    assert second.timings["ocr_resumed"] == 16
    assert [s["src"] for s in second.segments] == [f"大{i}" for i in range(20)]
    third = submit_and_wait(manager)  # the page is whole now: nothing is read at all
    assert third.status == "done" and ocr.reads == [[tuple(s[1]) for s in ocr.spans[16:]]] and third.timings.get("ocr_cache") is True
