from PIL import Image

from balloonlens import ocr_profile
from balloonlens.jobs import PIPELINE_CHUNK
from ocr_fakes import PageOcr


class Page(PageOcr):
    def __init__(self, count):
        self.count = count

    def run(self, img, src, reading):
        return [{"id": i, "bbox": [1, 2 + 10 * i, 3, 4], "src": f"大{i}", "conf": None} for i in range(self.count)], {}

    def read_spans(self, img, spans, src, stats=None):
        if stats is not None:
            stats["retry_spans"] = stats.get("retry_spans", 0) + 1
            stats["retry_fixed"] = stats.get("retry_fixed", 0) + 1
            stats["retry_ms"] = stats.get("retry_ms", 0) + 5
        return super().read_spans(img, spans, src, stats)


def test_a_page_is_planned_and_read_in_chunks_like_a_job_does_it():
    img = Image.new("RGB", (64, 64))
    p = ocr_profile.profile(Page(20), img, "ja", "rtl")
    assert (p["balloons"], p["chunks"], p["tiles"]) == (20, -(-20 // PIPELINE_CHUNK), 1)
    assert p["retry_spans"] == p["chunks"] and p["retry_fixed"] == p["chunks"] and p["retry_ms"] == 5 * p["chunks"]
    assert 0 <= p["first_regions"] <= p["total"] and p["detect"] == 0 and p["read"] <= p["total"]


def test_the_median_of_several_runs_and_the_table():
    img = Image.new("RGB", (64, 64))
    p = ocr_profile.median_profile(Page(3), img, "ja", "rtl", None, runs=3)
    text = ocr_profile.format_rows([("page.png", (64, 64), p)])
    lines = text.splitlines()
    assert "first regions" in lines[0] and "re-read" in lines[0]
    assert lines[1].startswith("page.png") and "64x64" in lines[1] and "1 (1 fixed)" in lines[1]
