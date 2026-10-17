"""Where a page's time goes, from metrics.jsonl (one row per finished job; no image and no dialogue text in it).

python -m balloonlens.metrics_report [--file PATH] [--last N] [--model NAME] [--json]

Per stage: how many jobs have it, and the median / 90th percentile / maximum in milliseconds. Below the table, what the numbers say:
which stage to work on first, how much the page pipeline overlaps reading and translating, and whether re-reads pay off.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

from . import config

# (key, what it measures). Times are from the job's creation unless it says otherwise.
STAGES = [
    ("queue_ms", "waiting in the queue"),
    ("gate_ms", "usage / login check before a Claude call"),
    ("decode_ms", "decoding the image"),
    ("memory_ms", "edits, work memory, reused lines"),
    ("ocr_lock_wait_ms", "waiting for the OCR lock (another page is being read)"),
    ("detect_ms", "finding the text boxes (CRAFT) and grouping them"),
    ("recognize_ms", "reading the balloons (Manga OCR / EasyOCR), re-reads included"),
    ("ocr_retry_ms", "of which re-reading doubtful balloons"),
    ("first_read_ms", "until the first balloons are read"),
    ("first_step_ms", "until the first translation step starts"),
    ("first_result_ms", "until the first line is translated"),
    ("visible_read_ms", "until the balloons on screen are read"),
    ("ocr_done_ms", "until the whole page is read"),
    ("translate_wait_ms", "waiting for translation steps after the page is read"),
    ("layout_ms", "applying the reader's layout"),
    ("present_ms", "bubble detection and cleanup"),
    ("translate_rest_ms", "translating what the steps did not cover"),
    ("translate_ms", "from the first translation step to the end"),
    ("cli_ms", "Claude CLI calls (sum)"),
    ("total_ms", "the whole job"),
]
COUNTS = ["segments", "boxes", "groups", "tiles", "steps", "ocr_retries", "ocr_retry_spans", "ocr_retry_fixed", "ocr_resumed"]
OCR_STAGES = ["queue_ms", "ocr_lock_wait_ms", "decode_ms", "memory_ms", "detect_ms", "recognize_ms"]


def load_rows(path: Path, last: int = 50, model: str | None = None) -> list[dict]:
    """The last `last` finished jobs (done or partial) of the file, oldest first. A line that is not JSON is skipped."""
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("status") in ("done", "partial") and (model is None or row.get("model") == model):
            rows.append(row)
    return rows[-last:] if last else rows


def percentile(values: list, q: float):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(rows: list[dict]) -> dict:
    """{key: {"n", "median", "p90", "max"}} for every stage and count that the rows have."""
    out = {}
    for key in [k for k, _ in STAGES] + COUNTS:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
        if values:
            out[key] = {"n": len(values), "median": statistics.median(values), "p90": percentile(values, 0.9), "max": max(values)}
    return out


def diagnose(rows: list[dict], s: dict) -> list[str]:
    """What the numbers say, in the order to act on them."""
    if not rows:
        return ["기록된 작업이 없어요."]
    med = lambda key: s[key]["median"] if key in s else None  # noqa: E731
    notes = []
    total = med("total_ms")
    # 1. Which stage before the translation is the largest. The review's rule: recognize -> Manga OCR, detect -> CRAFT,
    #    both small but ocr_done large -> the stages around them.
    ocr = {k: med(k) for k in OCR_STAGES if med(k) is not None}
    if ocr:
        worst = max(ocr, key=ocr.get)
        advice = {
            "recognize_ms": "글자 인식(Manga OCR)이 가장 길어요. 배치 크기와 재시도 횟수를 먼저 살펴보세요.",
            "detect_ms": "글자 위치 검출(CRAFT)이 가장 길어요. 긴 이미지의 타일 수와 검출기 비교를 먼저 살펴보세요.",
            "ocr_lock_wait_ms": "OCR 잠금 대기가 가장 길어요. 한 번에 여러 페이지를 요청하고 있어요. 현재 화면 이미지를 먼저 처리하는지 확인하세요.",
            "queue_ms": "큐 대기가 가장 길어요. 구독 호출 슬롯이나 앞선 작업이 막고 있어요.",
            "decode_ms": "이미지 디코딩이 가장 길어요. 이미지 크기를 확인하세요.",
            "memory_ms": "작품 기억 조회가 가장 길어요.",
        }
        notes.append(f"읽기 전 단계 중 가장 긴 것: {worst} (중앙값 {ocr[worst]:.0f} ms). {advice[worst]}")
    done, detect, recognize = med("ocr_done_ms"), med("detect_ms"), med("recognize_ms")
    if done is not None and detect is not None and recognize is not None and done > 1.5 * (detect + recognize) + 200:
        notes.append(f"ocr_done_ms({done:.0f})가 detect+recognize({detect + recognize:.0f})보다 훨씬 커요. "
                     "큐 대기, OCR 잠금 대기, 디코딩, 작품 기억 같은 다른 구간을 확인하세요.")
    # 2. Translation against reading, and how much the two overlap.
    translate = med("translate_ms")
    if translate is not None and done is not None:
        share = translate / (translate + done) if translate + done else 0
        notes.append(f"번역 구간 {translate:.0f} ms / 읽기 완료까지 {done:.0f} ms → 번역이 {share:.0%}를 차지해요."
                     + (" Claude 호출 시간이 병목이에요." if translate > done else ""))
    first, steps = med("first_result_ms"), med("steps")
    if first is not None and total:
        notes.append(f"첫 번역문까지 {first:.0f} ms, 작업 전체 {total:.0f} ms (첫 줄은 전체의 {first / total:.0%} 시점)."
                     + (f" 번역 호출은 페이지당 중앙값 {steps:.0f}번." if steps is not None else ""))
    # 3. Re-reads: how much of the reading they take, and how often they help.
    spans, fixed, retry_ms = sum(r.get("ocr_retry_spans", 0) for r in rows), sum(r.get("ocr_retry_fixed", 0) for r in rows), med("ocr_retry_ms")
    reads = sum(r.get("ocr_retries", 0) for r in rows)
    if spans:
        share = f", 읽기 시간의 {retry_ms / recognize:.0%}" if retry_ms and recognize else ""
        notes.append(f"의심 판독 {spans}개를 다시 읽었고(재인식 {reads}번) 그중 {fixed}개({fixed / spans:.0%})만 고쳐졌어요{share}. "
                     + ("고쳐지는 비율이 낮으면 재시도 조건을 줄일 여지가 있어요." if fixed / spans < 0.5 else "재시도는 효과가 있어요."))
    elif reads:
        notes.append(f"재인식 {reads}번 (고쳐진 비율 기록 없음: 새 버전 기록에만 있어요).")
    # 4. Pages that needed no reading at all.
    cached = sum(1 for r in rows if r.get("ocr_cache"))
    if cached:
        notes.append(f"{len(rows)}개 중 {cached}개는 OCR 캐시로 바로 번역했어요.")
    resumed = sum(r.get("ocr_resumed", 0) for r in rows)
    if resumed:
        notes.append(f"취소된 작업이 읽어 둔 말풍선 {resumed}개를 다음 작업이 이어받았어요.")
    return notes


def format_report(rows: list[dict], s: dict, notes: list[str]) -> str:
    lines = [f"작업 {len(rows)}개 (done/partial)", "", f"{'단계':20s} {'n':>4s} {'중앙값':>8s} {'p90':>8s} {'최대':>8s}  설명"]
    for key, what in STAGES:
        if key in s:
            v = s[key]
            lines.append(f"{key:20s} {v['n']:4d} {v['median']:8.0f} {v['p90']:8.0f} {v['max']:8.0f}  {what}")
    lines += ["", "건수: " + ", ".join(f"{k} {s[k]['median']:g}" for k in COUNTS if k in s) + " (중앙값)", "", "진단"]
    lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="balloonlens.metrics_report")
    ap.add_argument("--file", default=str(config.METRICS_PATH), help="metrics.jsonl (default: the helper's own)")
    ap.add_argument("--last", type=int, default=50, help="how many of the latest finished jobs (0 = all)")
    ap.add_argument("--model", default=None, help="only jobs with this model")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    rows = load_rows(Path(args.file), args.last, args.model)
    if not rows:
        print(f"{args.file}: 읽을 작업이 없어요.", file=sys.stderr)
        return 1
    s = summarize(rows)
    notes = diagnose(rows, s)
    print(json.dumps({"jobs": len(rows), "stages": s, "notes": notes}, ensure_ascii=False, indent=1) if args.json
          else format_report(rows, s, notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
