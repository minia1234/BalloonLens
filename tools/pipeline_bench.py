"""Does reading and translating at the same time pay off?  Real OCR, a fake translator that takes a set time per call.

    helper\\.venv\\Scripts\\python.exe tools\\pipeline_bench.py [images...] [--rows 8] [--call-s 3.0] [--line-s 0.1] [--rest-s 0] [--runs 2]

Each page runs twice through the helper's job queue: as one translator call after the whole page is read (the old order), and the way
the helper does it now: the first PIPELINE_CHUNK balloons go out early only if the rest takes PIPELINE_REST_S or more to read (default
2.5 s; --rest-s 0 splits every page of 20 balloons or more, to show what a split gains and costs). No Claude call is made. --call-s /
--line-s set how long the fake translator takes per call and per new line: the measured Claude time for short lines is about 2.7-3.2 s
per call. Without images, a page with --rows x 3 Japanese balloons is drawn. The numbers are this PC's.
"""
import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helper"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_synthetic as synth  # noqa: E402
from balloonlens import jobs  # noqa: E402
from balloonlens.cache import Cache  # noqa: E402
from balloonlens.ocr import OcrEngine, decode_image  # noqa: E402


class SlowTranslator:
    """Answers every new line after `call_s + line_s * new lines` seconds, as one call."""

    def __init__(self, call_s, line_s):
        self.call_s, self.line_s, self.calls = call_s, line_s, 0

    def translate_page(self, lines, src, tgt, model, scope, on_result, **options):
        fixed = options.get("fixed") or {}
        new = [(sid, text) for sid, text in lines if sid not in fixed]
        time.sleep(self.call_s + self.line_s * len(new))
        self.calls += 1
        for sid, text in new:
            on_result(sid, f"번역:{text}", "done")
        return {"load_ms": 0}


def dense_page(rows, seed=11, size=30):
    rng = random.Random(seed)
    w, h = 1000, max(1700, rows * 520)
    img = synth.background(w, h, rng)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(3):
            synth.draw_ja(d, rng.choice(synth.JA_LINES), int(w * (c + 0.5) / 3) + rng.randint(-20, 20),
                          int(h * (r + 0.5) / rows) + rng.randint(-20, 20), size)
    return img


def png_bytes(img):
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def run_once(ocr, data, chunk, rest_s, call_s, line_s, src, reading):
    old = jobs.PIPELINE_CHUNK, jobs.PIPELINE_REST_S
    jobs.PIPELINE_CHUNK, jobs.PIPELINE_REST_S = chunk, rest_s
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # the cache file stays open on Windows
            translator = SlowTranslator(call_s, line_s)
            manager = jobs.JobManager(ocr, translator, Cache(Path(tmp) / "c.db"), decode_image)
            manager.start()
            job = manager.submit(data, src=src, tgt="ko", model="qwen3.5:9b", reading=reading, priority=2, session="bench",
                                 scope="", options={})
            while job.status not in jobs.FINISHED:
                time.sleep(0.02)
            t = job.timings
            return {"balloons": len(job.segments), "calls": translator.calls, "first_read": t.get("first_read_ms"),
                    "first_result": t.get("first_result_ms"), "ocr_done": t.get("ocr_done_ms"), "total": t.get("total_ms")}
    finally:
        jobs.PIPELINE_CHUNK, jobs.PIPELINE_REST_S = old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--rows", type=int, default=8)
    ap.add_argument("--call-s", type=float, default=3.0)
    ap.add_argument("--line-s", type=float, default=0.1)
    ap.add_argument("--rest-s", type=float, default=jobs.PIPELINE_REST_S, help="PIPELINE_REST_S for the stepwise mode")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--src", default="ja")
    ap.add_argument("--reading", default="rtl")
    args = ap.parse_args()

    pages = [(Path(p).name, Path(p).read_bytes()) for p in args.images] or [(f"dense {args.rows}x3", png_bytes(dense_page(args.rows)))]
    ocr = OcrEngine()
    t0 = time.perf_counter()
    ocr.warmup()
    print(f"OCR ready in {time.perf_counter() - t0:.1f} s; fake translator {args.call_s} s per call + {args.line_s} s per new line\n")
    print(f"{'page':16s} {'mode':11s} {'balloons':>8s} {'calls':>5s} {'first read':>10s} {'first line':>10s} {'page read':>10s} {'total':>8s}  (ms, median of {args.runs})")
    for name, data in pages:
        for mode, chunk in (("whole page", 10 ** 9), ("in steps", jobs.PIPELINE_CHUNK)):
            runs = [run_once(ocr, data, chunk, args.rest_s, args.call_s, args.line_s, args.src, args.reading) for _ in range(args.runs)]
            med = lambda key: round(statistics.median(r[key] for r in runs))  # noqa: E731
            print(f"{name:16s} {mode:11s} {runs[0]['balloons']:8d} {runs[0]['calls']:5d} {med('first_read'):10d} {med('first_result'):10d} "
                  f"{med('ocr_done'):10d} {med('total'):8d}")


if __name__ == "__main__":
    main()
