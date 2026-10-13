import io
import threading
import time

from PIL import Image

from balloonlens.cache import Cache
from balloonlens.jobs import FINISHED, JobManager
from balloonlens.ocr import decode_image, visible_first
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

    def read_spans(self, img, spans, src):
        self.reads.append(len(spans))
        if len(self.reads) == 2:
            self.second_read.set()
        return super().read_spans(img, spans, src)


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


def test_a_long_page_is_translated_while_its_later_balloons_are_read(tmp_path):
    ocr = PagesOcr(count=10)
    translator = GatedTranslator(ocr)
    job = run_page(tmp_path, ocr, translator)
    assert job.status == "done", job.error
    assert translator.gate_passed is True  # the second chunk was read while the first one was translated
    assert ocr.reads == [8, 2]
    assert translator.pages[0] == 8
    assert translator.pages[1] == 10  # the 2 new balloons, with the 8 translated ones as reference
    assert [s["tgt"] for s in job.segments] == [f"번역:大{i}" for i in range(10)]
    assert job.timings["steps"] == 2


def test_a_short_page_is_one_translation_call_as_before(tmp_path):
    ocr = PagesOcr(count=3)
    translator = GatedTranslator(ocr, wait_s=0)
    job = run_page(tmp_path, ocr, translator)
    assert job.status == "done", job.error
    assert translator.pages == [3]
    assert ocr.reads == [3]


def test_the_band_the_reader_sees_is_read_first():
    # Three boxes top to bottom in reading order; the reader sees the lower two.
    boxes = [[0, 0, 10, 10], [0, 40, 10, 50], [0, 80, 10, 90]]
    assert visible_first([0, 1, 2], boxes, (35, 95)) == [1, 2, 0]
    assert visible_first([0, 1, 2], boxes, None) == [0, 1, 2]
    assert visible_first([0, 1, 2], boxes, (200, 300)) == [0, 1, 2]  # no box in the band: reading order stays
