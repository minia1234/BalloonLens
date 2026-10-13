"""Batched Japanese reading: a page with no balloons is not an error, and long pages are read in capped batches."""
from PIL import Image

from balloonlens.ocr import MANGA_BATCH, OcrEngine


def test_a_page_with_no_balloons_gives_no_segments():
    eng = OcrEngine(gpu=False)  # nothing is loaded until a model is asked for
    eng.detect = lambda img, src: []
    segments, timings = eng.run(Image.new("RGB", (800, 1000), "white"), "ja", "rtl")
    assert segments == []
    assert timings["groups"] == 0


def test_long_pages_are_read_in_capped_batches(monkeypatch):
    eng = OcrEngine(gpu=False)
    sizes = []

    def fake_batch(crops):
        sizes.append(len(crops))
        return ["読"] * len(crops)

    monkeypatch.setattr(eng, "_manga_batch", fake_batch)
    assert eng.manga_read([]) == []
    crops = [Image.new("RGB", (10, 10))] * (2 * MANGA_BATCH + 3)
    assert eng.manga_read(crops) == ["読"] * (2 * MANGA_BATCH + 3)
    assert sizes == [MANGA_BATCH, MANGA_BATCH, 3]
