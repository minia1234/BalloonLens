"""Selective vs full Qwen3.5 review on identical OCR input.

Measures model calls, proofread lines and time, and lists every line where the two modes produce
different Korean. A difference is not a verdict: a person has to judge which reading is right.

Usage (project root):  helper\\.venv\\Scripts\\python.exe tools\\review_compare.py [images or folders]
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helper"))

from balloonlens.cache import Cache  # noqa: E402
from balloonlens.ocr import OcrEngine, decode_image  # noqa: E402
from balloonlens.translate import Translator  # noqa: E402

MODES = ("selective", "all")


def images(inputs):
    for item in map(Path, inputs):
        if item.is_dir():
            yield from sorted(p for p in item.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        else:
            yield item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", default=[str(ROOT / "samples" / "synthetic")])
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--instructions", default="")
    ap.add_argument("--glossary", default="")
    args = ap.parse_args()

    out = ROOT / "bench_out" / "review-compare" / str(time.time_ns())
    out.mkdir(parents=True)
    engine = OcrEngine()
    engine.warmup()
    translator = Translator(Cache(out / "scratch.sqlite3"))
    # A throwaway page translation loads the model with the same prompt shape before anything is timed
    # (a plain "ok" chat left the first measured page several seconds slow).
    translator.translate_page([(0, "テスト"), (1, "3時に")], "ja", "ko", args.model, "warmup",
                              lambda *a: None, use_cache=False, review="all")

    pages = []
    for n, path in enumerate(images(args.inputs)):
        truth = path.with_name(path.stem + ".truth.json")
        meta = json.loads(truth.read_text(encoding="utf-8")) if truth.exists() else {}
        src, reading = meta.get("src", "ja"), meta.get("reading", "rtl")
        segments, _ = engine.run(decode_image(path.read_bytes()), src, reading)
        page = {"file": path.name, "src": src, "segments": segments, "modes": {}}
        # Alternate which mode runs first so a warm-up effect doesn't favour one side.
        for mode in (MODES if n % 2 == 0 else MODES[::-1]):
            texts = {}
            started = time.perf_counter()
            result = translator.translate_page(
                [(s["id"], s["src"]) for s in segments], src, "ko", args.model, "review-compare",
                lambda sid, text, status: texts.update({sid: {"text": text, "status": status}}),
                use_cache=False, review=mode, instructions=args.instructions, glossary=args.glossary,
                uncertain={s["id"] for s in segments if s.get("ocr_uncertain")})
            page["modes"][mode] = {
                "seconds": round(time.perf_counter() - started, 2), "calls": result["calls"],
                "proofread_lines": result.get("proofread_lines", 0),
                "proofread_skipped": result.get("proofread_skipped", 0), "texts": texts,
            }
            print(f"{path.name} {mode}: {page['modes'][mode]['seconds']}s, {result['calls']} calls", flush=True)
        pages.append(page)
        (out / "results.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")

    total = {m: {k: sum(p["modes"][m][k] for p in pages) for k in ("seconds", "calls", "proofread_lines")} for m in MODES}
    lines_total = sum(len(p["segments"]) for p in pages)
    report = [
        "# Selective vs full review", "",
        f"Model `{args.model}`, {len(pages)} images, {lines_total} lines. Same OCR input for both modes; no cache.", "",
        "| mode | seconds | model calls | proofread lines |", "|---|---|---|---|",
        *[f"| {m} | {total[m]['seconds']:.1f} | {total[m]['calls']} | {total[m]['proofread_lines']} |" for m in MODES],
        "", "## Lines that differ", "",
        "Different wording is not proof that either side is wrong. Judge each row.", "",
        "| file | source | selective | all |", "|---|---|---|---|",
    ]
    differ = 0
    for p in pages:
        for s in p["segments"]:
            a = p["modes"]["selective"]["texts"].get(s["id"], {}).get("text") or ""
            b = p["modes"]["all"]["texts"].get(s["id"], {}).get("text") or ""
            if a != b:
                differ += 1
                cell = lambda v: v.replace("|", "/").replace("\n", " ")
                report.append(f"| {p['file']} | {cell(s['src'])} | {cell(a)} | {cell(b)} |")
    if not differ:
        report.append("| (none) | | | |")
    report += ["", f"{differ} of {lines_total} lines differ."]
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report[4:8]))
    print(f"{differ}/{lines_total} lines differ · {out / 'report.md'}")


if __name__ == "__main__":
    main()
