"""Translation benchmark across backends (plan §17.1): OCR once per image, then each backend.

python -m balloonlens.bench <images...|dir> --src ja --backends local:translategemma:4b,local:qwen3:8b
python -m balloonlens.bench <images...|dir> --backends claude,codex --live

  local:<model>   Ollama model. Reports the cold load time and the VRAM peak.
  claude, codex   Subscription CLI backends. They spend subscription usage, so they run only with --live.
                  Token counts come from the CLI's own usage report. Codex runs only after its sandbox check passed.

Optional ground truth next to each image: <name>.truth.json  {"src", "reading", "lines": [...]}
Report contains dialogue text and stays on this PC.
"""
import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config
from .backends import ClaudeCodeBackend, CliTranslator, CodexBackend, QuotaPolicy
from .cache import Cache
from .ocr import OcrEngine, decode_image
from .translate import Translator, ollama_unload

FONT = "C:/Windows/Fonts/malgun.ttf"
SUBSCRIPTION = {"claude": config.CLAUDE_MODEL, "codex": config.CODEX_MODEL}


class VramSampler:
    def __init__(self):
        self.peak, self._stop = 0, threading.Event()

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                self.peak = max(self.peak, int(out.split()[0]))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                return
            self._stop.wait(0.25)

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def __exit__(self, *_):
        self._stop.set()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def norm(s: str) -> str:
    return "".join(s.split()).upper()


def ocr_score(truth_lines, segments):
    """Best-match character error rate per truth line; CER > 0.5 counts as missed."""
    found = [norm(s["src"]) for s in segments]
    cers = []
    for line in truth_lines:
        t = norm(line)
        cers.append(min((edit_distance(t, f) / max(len(t), 1) for f in found), default=1.0))
    return {"mean_cer": round(statistics.mean(cers), 3) if cers else None,
            "missed": sum(c > 0.5 for c in cers), "truth_lines": len(truth_lines)}


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def preview(img: Image.Image, segments, texts: dict, path: Path):
    im = img.copy()
    d = ImageDraw.Draw(im)
    for s in segments:
        x, y, w, h = s["bbox"]
        d.rectangle([x, y, x + w, y + h], outline=(255, 0, 80), width=3)
        size = max(14, min(28, int(max(w, h) / 8)))
        font = ImageFont.truetype(FONT, size)
        d.text((x, max(0, y - size - 4)), f"{s['id']}: {texts.get(s['id']) or '∅'}", font=font, fill=(0, 90, 255),
               stroke_width=3, stroke_fill="white")
    im.save(path)


def parse_backends(text: str) -> list[tuple[str, str | None]]:
    """'local:qwen3:8b,claude' -> [('local', 'qwen3:8b'), ('claude', None)]. Ollama model names keep their colon."""
    specs = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if item.startswith("local:"):
            specs.append(("local", item[len("local:"):]))
        elif item in SUBSCRIPTION:
            specs.append((item, None))
        else:
            raise ValueError(f"unknown backend: {item} (use local:<model>, claude or codex)")
    return specs


def subscription_translator(name: str, cache: Cache) -> CliTranslator:
    """The same backend objects the helper builds (see __main__.build_router), with the bench's own usage ledger."""
    quota = QuotaPolicy(cache)
    if name == "claude":
        backend = ClaudeCodeBackend(command=config.claude_command(), config_dir=config.CLAUDE_CONFIG_DIR,
                                    sandbox_dir=config.CLAUDE_SANDBOX_DIR, runtime_dir=config.RUNTIME_DIR,
                                    model=config.CLAUDE_MODEL, effort=config.CLAUDE_EFFORT, timeout=config.CLI_TIMEOUT_S)
    else:
        # Read the real sandbox verdict: the bench never runs Codex on a machine where its sandbox failed the check.
        verdict = Cache(config.DB_PATH).get_flags("codex").get("verdict", "")
        if verdict != "read_blocked":
            raise SystemExit("Codex needs a passing sandbox check first (설정 화면의 보안 점검).")
        backend = CodexBackend(command=config.codex_command(), sandbox_dir=config.CODEX_SANDBOX_DIR,
                               runtime_dir=config.RUNTIME_DIR, model=config.CODEX_MODEL,
                               reasoning=config.CODEX_REASONING, timeout=config.CLI_TIMEOUT_S,
                               sandbox_verdict=verdict)
    return CliTranslator(backend, cache, quota)


def main():
    ap = argparse.ArgumentParser(prog="balloonlens.bench")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--src", default=None, help="ja|en (default: from truth file)")
    ap.add_argument("--tgt", default="ko")
    ap.add_argument("--reading", default=None, help="rtl|ltr|ttb (default: from truth file or rtl)")
    ap.add_argument("--backends", default="local:translategemma:4b,local:qwen3:8b")
    ap.add_argument("--live", action="store_true", help="allow claude and codex (they spend subscription usage)")
    ap.add_argument("--out", default="bench_out")
    args = ap.parse_args()
    specs = parse_backends(args.backends)
    if any(kind in SUBSCRIPTION for kind, _ in specs) and not args.live:
        sys.exit("claude and codex spend subscription usage. Add --live to run them.")

    paths = []
    for inp in map(Path, args.inputs):
        paths += sorted(p for p in inp.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}) \
            if inp.is_dir() else [inp]
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    scratch = Cache(out / "scratch.sqlite3")  # no cache hits, and the reader's data is not touched

    engine = OcrEngine()
    t = time.perf_counter()
    engine.warmup()
    ocr_load_ms = round((time.perf_counter() - t) * 1000)

    pages = []
    for p in paths:
        truth_path = p.with_name(p.stem + ".truth.json")
        truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {}
        src = args.src or truth.get("src") or "ja"
        reading = args.reading or truth.get("reading") or ("rtl" if src == "ja" else "ltr")
        img = decode_image(p.read_bytes())
        t = time.perf_counter()
        segments, timings = engine.run(img, src, reading)
        timings["ocr_ms"] = round((time.perf_counter() - t) * 1000)
        page = {"file": p.name, "src": src, "size": [img.width, img.height], "chars": sum(len(s["src"]) for s in segments),
                "segments": segments, "ocr": timings, "translations": {}}
        if truth.get("lines"):
            page["ocr_score"] = ocr_score(truth["lines"], segments)
        pages.append((page, img))
        print(f"OCR {p.name}: {len(segments)} segments, {timings['ocr_ms']} ms")

    summary, labels = {}, []
    for kind, model in specs:
        label = model if kind == "local" else SUBSCRIPTION[kind]
        labels.append(label)
        if kind == "local":
            ollama_unload(model)  # so the first call measures a cold load
            tr = Translator(scratch)
            call = tr.translate
        else:
            tr = subscription_translator(kind, scratch)
            call = tr.translate_page
        first_line_ms, page_ms, load_ms, failures, calls = [], [], [], 0, 0
        usage = {"input": 0, "output": 0, "cached": 0}
        cold_ms = None
        with VramSampler() as vram:
            for page, img in pages:
                texts, stamps = {}, []
                t0 = time.perf_counter()

                def on_result(sid, text, status, texts=texts, stamps=stamps, t0=t0, page=page, label=label):
                    texts[sid] = text
                    stamps.append((time.perf_counter() - t0) * 1000)
                    page["translations"].setdefault(label, {})[sid] = {"text": text, "status": status}

                meta = call([(s["id"], s["src"]) for s in page["segments"]], page["src"], args.tgt, label, "bench",
                            on_result, use_cache=False)
                elapsed = (time.perf_counter() - t0) * 1000
                load_ms.append(meta.get("load_ms", 0))
                calls += meta.get("calls", 0)
                for key, value in (meta.get("usage") or {}).items():
                    usage[key] = usage.get(key, 0) + value
                if cold_ms is None:
                    cold_ms = round(elapsed) if kind != "local" else meta.get("load_ms", 0)
                if stamps:
                    first_line_ms.append(stamps[0] - meta.get("load_ms", 0) if len(load_ms) == 1 else stamps[0])
                page_ms.append(elapsed - (meta.get("load_ms", 0) if len(load_ms) == 1 else 0))
                failures += sum(1 for v in page["translations"].get(label, {}).values() if v["status"] != "done")
                page.setdefault("translate_ms", {})[label] = round(elapsed)
                preview(img, page["segments"], texts, out / f"{Path(page['file']).stem}__{label.replace(':', '_')}.png")
                print(f"  {label} {page['file']}: {round(elapsed)} ms" + (f"  ERROR {meta['error']}" if meta.get("error") else ""))
        local = kind == "local"
        summary[label] = {
            "kind": kind,
            "cold_ms": cold_ms,
            "first_line_warm_ms_median": round(statistics.median(first_line_ms)) if first_line_ms else None,
            "page_warm_ms_median": round(statistics.median(page_ms)) if page_ms else None,
            "page_warm_ms_p95": round(pct(page_ms, 0.95)) if page_ms else None,
            "not_done_lines": failures,
            "vram_peak_mib": vram.peak if local else None,
            "calls": None if local else calls,
            "tokens": None if local else usage,
        }
        if local:
            ollama_unload(model)  # don't stack two models in VRAM

    report = {"ocr_load_ms": ocr_load_ms, "models": summary, "pages": [p for p, _ in pages]}
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    write_markdown(out / "report.md", report, labels)
    print(f"\nreport: {out / 'report.md'}")


def dash(value):
    return "-" if value is None else value


def write_markdown(path: Path, report: dict, labels: list[str]):
    L = ["# BalloonLens backend bench", "", f"OCR model load: {report['ocr_load_ms']} ms", "",
         "## Backends", "",
         "| backend | cold ms | first line (warm) ms | page median ms | page p95 ms | not-done lines | VRAM peak MiB "
         "| calls | tokens in | tokens out | tokens cached |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, s in report["models"].items():
        tok = s.get("tokens") or {}
        L.append(f"| {label} | {s['cold_ms']} | {s['first_line_warm_ms_median']} | {s['page_warm_ms_median']} | "
                 f"{s['page_warm_ms_p95']} | {s['not_done_lines']} | {dash(s['vram_peak_mib'])} | {dash(s['calls'])} | "
                 f"{dash(tok.get('input'))} | {dash(tok.get('output'))} | {dash(tok.get('cached'))} |")
    L += ["", "## Pages", "", "| file | size | segments | chars | detect ms | recognize ms | CER | missed/truth |",
          "|---|---|---|---|---|---|---|---|"]
    for p in report["pages"]:
        sc = p.get("ocr_score", {})
        L.append(f"| {p['file']} | {p['size'][0]}x{p['size'][1]} | {len(p['segments'])} | {p['chars']} | "
                 f"{p['ocr']['detect_ms']} | {p['ocr']['recognize_ms']} | {sc.get('mean_cer', '-')} | "
                 f"{sc.get('missed', '-')}/{sc.get('truth_lines', '-')} |")
    L += ["", "## Lines (fill the error columns by hand: OCR / TR / placement)", ""]
    for p in report["pages"]:
        L += [f"### {p['file']}", "", "| id | source | " + " | ".join(labels) + " | OCR err | TR err | place err |",
              "|---|---|" + "---|" * len(labels) + "---|---|---|"]
        for s in p["segments"]:
            cells = []
            for label in labels:
                v = p["translations"].get(label, {}).get(s["id"], {})
                mark = "" if v.get("status") == "done" else f" _({v.get('status', '-')})_"
                cells.append(f"{(v.get('text') or '').replace('|', '/')}{mark}")
            L.append(f"| {s['id']} | {s['src'].replace('|', '/')} | " + " | ".join(cells) + " |  |  |  |")
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
