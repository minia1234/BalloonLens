"""How long the OCR takes on your own pages, stage by stage, with no translation:

    .venv\\Scripts\\python.exe -m balloonlens.ocr_profile page1.png page2.jpg [--src ja] [--reading rtl] [--runs 3] [--visible 0.4,0.7]

Run it in the helper folder. Each page is planned and read the way a job does it: detection first (tile by tile on a tall page), then
the balloons PIPELINE_CHUNK at a time. --visible gives the part of the page on screen as fractions of its height. The time is the
median of --runs after one run that is not counted (the first run on a GPU is slow). Pages and text stay on this PC.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

from .jobs import PIPELINE_CHUNK
from .ocr import OcrEngine, decode_image


def profile(engine, img, src: str, reading: str, band=None) -> dict:
    """One pass over a page. Times in ms: first_regions (until the first balloons are ready to read), detect, read (all chunks),
    total. retry_* are the engine's own counts for the doubtful balloons it read again."""
    t0 = time.perf_counter()
    first, read_ms, chunks, stats, queue, final = None, 0.0, [], {}, [], None
    for step in engine.plan_steps(img, src, reading, band):
        if first is None and step.spans:
            first = (time.perf_counter() - t0) * 1000
        queue += step.spans
        while queue and (len(queue) >= PIPELINE_CHUNK or step.final):
            chunk, queue = queue[:PIPELINE_CHUNK], queue[PIPELINE_CHUNK:]
            t = time.perf_counter()
            engine.read_spans(img, chunk, src, stats)
            chunks.append(len(chunk))
            read_ms += (time.perf_counter() - t) * 1000
        if step.final:
            final = step
    return {"tiles": final.stats["tiles"], "boxes": final.stats["boxes"], "groups": final.stats["groups"],
            "balloons": sum(chunks), "chunks": len(chunks), "first_regions": first or 0.0, "detect": float(final.stats["detect_ms"]),
            "read": read_ms, "retry_spans": stats.get("retry_spans", 0), "retry_fixed": stats.get("retry_fixed", 0),
            "retry_ms": float(stats.get("retry_ms", 0)), "total": (time.perf_counter() - t0) * 1000}


def median_profile(engine, img, src, reading, band, runs: int) -> dict:
    profile(engine, img, src, reading, band)  # not counted: the first pass on a GPU sets up its kernels
    rows = [profile(engine, img, src, reading, band) for _ in range(runs)]
    return {k: statistics.median(r[k] for r in rows) for k in rows[0]}


def format_rows(results: list[tuple[str, tuple, dict]]) -> str:
    head = (f"{'page':24s} {'size':>10s} {'tiles':>5s} {'balloons':>8s} {'first regions':>13s} {'detect':>7s} {'read':>7s} "
            f"{'re-read':>12s} {'total':>7s}   (ms)")
    lines = [head]
    for name, size, p in results:
        retry = f"{p['retry_spans']:g} ({p['retry_fixed']:g} fixed)"
        lines.append(f"{name[:24]:24s} {size[0]}x{size[1]:<5d} {p['tiles']:5g} {p['balloons']:8g} {p['first_regions']:13.0f} "
                     f"{p['detect']:7.0f} {p['read']:7.0f} {retry:>12s} {p['total']:7.0f}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="balloonlens.ocr_profile")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--src", default="ja", choices=["ja", "en"])
    ap.add_argument("--reading", default=None, help="rtl|ltr|ttb (default: rtl for ja, ltr for en)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--visible", default=None, help="top,bottom of the part on screen, as fractions of the page height")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args(argv)
    reading = args.reading or ("rtl" if args.src == "ja" else "ltr")
    fractions = tuple(float(v) for v in args.visible.split(",")) if args.visible else None
    engine = OcrEngine(gpu=not args.cpu)
    t0 = time.perf_counter()
    engine.warmup()
    print(f"models ready in {time.perf_counter() - t0:.1f} s\n")
    results = []
    for path in map(Path, args.images):
        img = decode_image(path.read_bytes())
        band = (fractions[0] * img.height, fractions[1] * img.height) if fractions else None
        results.append((path.name, (img.width, img.height), median_profile(engine, img, args.src, reading, band, args.runs)))
    print(format_rows(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
