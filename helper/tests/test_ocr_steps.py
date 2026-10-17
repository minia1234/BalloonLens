"""The OCR engine's steps: tile-by-tile planning, batched re-reads of doubtful balloons, one-call English reading, and the GPU lock.
No model is loaded: the detector and the readers are replaced by small fakes."""
import random
import threading

import pytest
from PIL import Image

from balloonlens.ocr import (OcrEngine, clean_rects, group_reach, in_reading_order, spans_of, tile_order, tile_ranges,
                             touches_pending)


def page(width=800, height=6000):
    return Image.new("RGB", (width, height), "white")


def engine_with_text_at(boxes, calls=None):
    """An engine whose detector 'sees' the given page boxes: each tile returns the boxes that overlap it, clipped to the tile
    (a text line cut by a tile edge is found as a shorter box, as CRAFT does)."""
    eng = OcrEngine(gpu=False)

    def detect_tile(arr, tile, src):
        y0, y1 = tile
        if calls is not None:
            calls.append(tile)
        return [[b[0], max(b[1], y0), b[2], min(b[3], y1)] for b in boxes if b[1] < y1 and b[3] > y0 and min(b[3], y1) - max(b[1], y0) >= 4]

    eng._detect_tile = detect_tile
    return eng


def collect(eng, img, visible=None, reading="ltr"):
    steps = list(eng.plan_steps(img, "en", reading, visible))
    assert [s.final for s in steps] == [False] * (len(steps) - 1) + [True]
    return steps


def box_of(span):
    return tuple(span[1])


# ---------- tiles ----------

def test_tiles_touching_the_band_are_detected_first():
    tiles = tile_ranges(800, 6000)
    assert tile_order(tiles, None) == list(range(len(tiles)))
    order = tile_order(tiles, (3000, 3300))
    near = [i for i, (y0, y1) in enumerate(tiles) if y0 < 3300 and y1 > 3000]
    assert order[:len(near)] == near and sorted(order) == list(range(len(tiles)))
    assert tile_order(tiles, (-100, -10)) == list(range(len(tiles)))  # nothing at the band: page order


def test_a_region_waits_while_a_tile_beside_it_is_undetected():
    assert touches_pending([0, 100, 50, 200], [(190, 400)], reach=20)
    assert not touches_pending([0, 100, 50, 200], [(260, 400)], reach=20)
    assert group_reach([[0, 0, 30, 100], [0, 0, 60, 90]]) == pytest.approx(0.6 * 60 + 4)  # the largest of the boxes' smaller sides
    assert group_reach([]) == 4


def test_a_one_tile_page_is_planned_in_one_step():
    eng = engine_with_text_at([[10, 100, 60, 200]])
    steps = collect(eng, page(800, 1200))
    assert len(steps) == 1
    assert steps[0].spans == [([[10, 100, 60, 200]], [10, 100, 60, 200])]


def test_regions_far_from_the_undetected_tiles_are_given_early_and_the_last_step_completes_the_page():
    img = page()
    boxes = [[100, 300, 150, 400], [100, 2700, 150, 2800], [100, 5500, 150, 5600]]
    calls = []
    eng = engine_with_text_at(boxes, calls)
    tiles = tile_ranges(img.width, img.height)
    steps = collect(eng, img, visible=(2600, 2900))
    assert calls[0] == tiles[2]  # the tile that holds the band is detected first
    early = [box_of(s) for step in steps[:-1] for s in step.spans]
    assert (100, 2700, 150, 2800) in early
    final = steps[-1]
    given = early + [box_of(s) for s in final.spans]
    assert sorted(given) == sorted(tuple(b) for b in boxes) and len(given) == len(set(given))
    # The visible region leads, then the rest in reading order.
    assert final.order[0] == [box_of(s) for s in final.all_spans].index((100, 2700, 150, 2800))
    assert final.order == in_reading_order(final.all_spans, "ltr", (2600, 2900))


def test_the_last_step_has_the_same_page_as_one_pass_over_all_the_tiles():
    rng = random.Random(3)
    img = page(800, 9000)
    for _ in range(25):
        boxes = []
        for _ in range(rng.randint(3, 14)):
            x, y = rng.randint(0, 700), rng.randint(0, 8800)
            boxes.append([x, y, x + rng.randint(20, 60), y + rng.randint(30, 400)])
        eng = engine_with_text_at(boxes)
        band = (rng.randint(0, 8000), rng.randint(8001, 9000)) if rng.random() < 0.7 else None
        steps = collect(eng, img, band)
        rects = eng.detect(img, "en")  # all the tiles, in page order
        expected, groups = spans_of(rects)
        final = steps[-1]
        assert [box_of(s) for s in final.all_spans] == [tuple(s[1]) for s in expected]
        assert final.stats["groups"] == groups and final.stats["tiles"] == len(tile_ranges(800, 9000))
        # Every region of the page is given exactly once, early or in the last step, unless a later tile made it part of a larger one.
        given = [box_of(s) for step in steps for s in step.spans]
        assert len(given) == len(set(given))
        assert {box_of(s) for s in final.all_spans} <= set(given)


def test_plan_gives_what_the_last_step_has():
    eng = engine_with_text_at([[10, 100, 60, 200], [10, 3000, 60, 3100]])
    spans, order, stats = eng.plan(page(), "en", "ltr")
    last = collect(eng, page())[-1]
    without_time = lambda s: {k: v for k, v in s.items() if k != "detect_ms"}  # noqa: E731
    assert spans == last.all_spans and order == last.order and without_time(stats) == without_time(last.stats)


# ---------- re-reads ----------

def make_reading_engine(first, variants):
    """An engine for Japanese spans: the first reading of each span comes from `first`, and a re-read gives the next text of
    `variants[i]`. Records every batch the model gets."""
    eng = OcrEngine(gpu=False)
    batches = []
    texts = iter(first)

    def manga_read(crops):
        batches.append(len(crops))
        if len(batches) == 1:
            return [next(texts) for _ in crops]
        return [variants.pop(0) for _ in crops]

    eng.manga_read = manga_read
    return eng, batches


def spans_for(n):
    """Tall narrow boxes, like Japanese columns: each holds about five characters."""
    return [([[10, 20 + 120 * i, 30, 120 + 120 * i]], [10, 20 + 120 * i, 30, 120 + 120 * i]) for i in range(n)]


def test_doubtful_balloons_are_re_read_together_in_one_batch_per_variant():
    # Balloons 1 and 3 are doubtful (a lone dash in a five-character column). Their first variants are read in one batch: balloon 1 is
    # fixed by it, balloon 3 is still doubtful and goes into a second batch alone.
    variants = ["はい、わかりました", "．．．", "どこでもない"]
    eng, batches = make_reading_engine(["待ってくれ", "ー", "どこへ行くの", "ー"], variants)
    stats = {}
    out = eng.read_spans(Image.new("RGB", (200, 700), "white"), spans_for(4), "ja", stats)
    assert batches == [4, 2, 1]
    assert [o[1] for o in out] == ["待ってくれ", "はい、わかりました", "どこへ行くの", "どこでもない"]
    assert [o[4] for o in out] == [0, 1, 0, 2]
    assert [o[3] for o in out] == [[], [], [], []]
    assert stats["retry_spans"] == 2 and stats["retry_fixed"] == 2 and stats["retry_ms"] >= 0


def test_a_page_without_doubts_is_not_re_read():
    eng, batches = make_reading_engine(["待ってくれ", "どこへ行くの"], [])
    out = eng.read_spans(Image.new("RGB", (200, 700), "white"), spans_for(2), "ja")
    assert batches == [2] and [o[4] for o in out] == [0, 0]


def test_a_balloon_that_stays_doubtful_keeps_its_best_reading_and_is_marked():
    eng, batches = make_reading_engine(["ー"], ["．．", "．．．"])
    stats = {}
    [(box, text, conf, doubts, retries)] = eng.read_spans(Image.new("RGB", (200, 700), "white"), spans_for(1), "ja", stats)
    assert batches == [1, 1, 1] and retries == 2 and doubts and text == "ー"
    assert stats["retry_spans"] == 1 and stats["retry_fixed"] == 0


def test_no_spans_read_nothing():
    eng, batches = make_reading_engine([], [])
    assert eng.read_spans(Image.new("RGB", (10, 10)), [], "ja") == [] and batches == []


# ---------- English in one call ----------

class FakeReader:
    def __init__(self):
        self.calls = []

    def recognize(self, grey, horizontal_list, free_list, detail):
        self.calls.append(len(horizontal_list))
        # EasyOCR returns the lines sorted by their top edge, each with its box corners.
        lines = sorted(horizontal_list, key=lambda b: b[2])
        return [([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], f"line{x0}.{y0}", 0.9) for x0, x1, y0, y1 in lines]


def test_english_groups_are_read_in_one_recognizer_call_and_land_in_their_group():
    eng = OcrEngine(gpu=False)
    eng._reader = reader = FakeReader()
    eng._manga = object()
    spans = [([[10, 100, 80, 130], [10, 140, 80, 170]], [10, 100, 80, 170]), ([[200, 20, 300, 60]], [200, 20, 300, 60])]
    out = eng.read_spans(Image.new("RGB", (400, 400), "white"), spans, "en")
    assert reader.calls == [3]
    assert [o[1] for o in out] == ["line10.100 line10.140", "line200.20"]
    assert eng.ready


# ---------- GPU lock and readiness ----------

def test_a_page_read_waits_for_the_warm_up_pass():
    eng = OcrEngine(gpu=False)
    eng._reader = type("R", (), {"readtext": lambda self, *a, **k: []})()
    eng._manga = lambda crop: ""
    inside, release, calls = threading.Event(), threading.Event(), []

    def detect(img, src):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:  # the warm-up pass
            inside.set()
            release.wait(5)
        return []

    eng.detect = detect
    prime = threading.Thread(target=eng.prime, name="prime")
    prime.start()
    assert inside.wait(5)
    reader = threading.Thread(target=lambda: eng.plan(page(100, 100), "ja", "rtl"), name="page")
    reader.start()
    reader.join(0.3)
    assert reader.is_alive() and calls == ["prime"]  # the page read waits behind the warm-up pass
    release.set()
    prime.join(5)
    reader.join(5)
    assert calls == ["prime", "page"] and eng.ready


def test_the_engine_is_not_ready_until_a_pass_has_run_even_with_both_models_loaded():
    eng = OcrEngine(gpu=False)
    eng._reader, eng._manga = object(), object()
    assert not eng.ready
    eng._primed = True
    assert eng.ready


def test_run_holds_the_lock_while_it_plans_and_reads():
    eng = OcrEngine(gpu=False)
    eng.detect = lambda img, src: []
    segments, timings = eng.run(page(100, 100), "ja", "rtl")  # run() -> plan() -> the same re-entrant lock
    assert segments == [] and timings["groups"] == 0


def test_clean_rects_clip_and_merge():
    assert clean_rects([[-5, -5, 40, 40], [30, 30, 33, 90], [10, 10, 38, 38]], 100, 100) == [[0, 0, 40, 40]]
