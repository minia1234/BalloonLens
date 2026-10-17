"""Text detection -> line grouping -> recognition (section 8).

Detection: EasyOCR's CRAFT detector (language-agnostic boxes).
Grouping: nearby boxes are merged into one dialogue region (roughly one balloon).
Recognition: Manga OCR for Japanese, EasyOCR recognizer for English.
"""
import io
import re
import threading
import time
from statistics import median
from typing import NamedTuple

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import config

Rect = list  # [x0, y0, x1, y1] in image pixels
MANGA_BATCH = 8  # balloons per Japanese generate call


class ImageTooLarge(ValueError):
    pass


def decode_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    if img.width * img.height > config.MAX_IMAGE_PIXELS:
        raise ImageTooLarge(f"{img.width}x{img.height}")
    return img.convert("RGB")


# ---------- pure geometry (unit-tested) ----------

def tile_ranges(width: int, height: int) -> list[tuple[int, int]]:
    """Split tall (webtoon) images into overlapping vertical tiles so text isn't shrunk away."""
    tile_h = max(int(width * 1.5), 1200)
    if height <= tile_h * 1.2:
        return [(0, height)]
    overlap = max(int(tile_h * 0.15), 150)
    ranges, y = [], 0
    while True:
        end = min(y + tile_h, height)
        ranges.append((y, end))
        if end >= height:
            return ranges
        y = end - overlap


def group_boxes(rects: list[Rect], gap_ratio: float = 0.6) -> list[list[int]]:
    """Union boxes whose gap is smaller than gap_ratio x character size. Returns index groups."""
    n = len(rects)
    if n == 0:
        return []
    sizes = [min(r[2] - r[0], r[3] - r[1]) for r in rects]
    floor = 0.5 * median(sizes)
    pads = [gap_ratio * max(s, floor) / 2 for s in sizes]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a, pa = rects[i], pads[i]
        for j in range(i + 1, n):
            b, pb = rects[j], pads[j]
            p = pa + pb
            if a[0] - p <= b[2] and b[0] - p <= a[2] and a[1] - p <= b[3] and b[1] - p <= a[3]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def dedupe_rects(rects: list[Rect], threshold: float = 0.5) -> list[Rect]:
    """Merge boxes that mostly cover each other (the same line seen by two overlapping tiles)."""
    out: list[Rect] = []
    for r in sorted(rects, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        area_r = (r[2] - r[0]) * (r[3] - r[1])
        for k, o in enumerate(out):
            iw, ih = min(r[2], o[2]) - max(r[0], o[0]), min(r[3], o[3]) - max(r[1], o[1])
            if iw > 0 and ih > 0 and iw * ih >= threshold * min(area_r, (o[2] - o[0]) * (o[3] - o[1])):
                out[k] = union_rect([o, r])
                break
        else:
            out.append(list(r))
    return out


def union_rect(rects: list[Rect]) -> Rect:
    return [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]


def order_rects(rects: list[Rect], reading: str) -> list[int]:
    """Reading order: rows top->bottom; inside a row right->left (rtl) or left->right (ltr/ttb)."""
    rows: list[dict] = []
    for i in sorted(range(len(rects)), key=lambda k: rects[k][1]):
        r = rects[i]
        h = r[3] - r[1]
        for row in rows:
            overlap = min(r[3], row["y1"]) - max(r[1], row["y0"])
            if overlap >= 0.5 * min(h, row["y1"] - row["y0"]):
                row["items"].append(i)
                row["y0"], row["y1"] = min(row["y0"], r[1]), max(row["y1"], r[3])
                break
        else:
            rows.append({"y0": r[1], "y1": r[3], "items": [i]})
    order = []
    for row in sorted(rows, key=lambda rw: rw["y0"]):
        if reading == "rtl":
            order += sorted(row["items"], key=lambda k: -rects[k][2])
        else:
            order += sorted(row["items"], key=lambda k: rects[k][0])
    return order


# ---------- doubtful readings (unit-tested) ----------

_JA_LETTER = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾝA-Za-z0-9０-９Ａ-Ｚａ-ｚ]")
_EN_LETTER = re.compile(r"[A-Za-z0-9]")
_PUNCT_ONLY = re.compile(r"[.…!?！？。・~〜．]{1,6}")


def _join_lines(results):
    lines = []
    for pts, text, conf in results:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        lines.append(((min(ys) + max(ys)) / 2, min(xs), text, conf))
    # Reading order: by line (bucketed by height), then x.
    lines.sort(key=lambda t: (round(t[0] / 8), t[1]))
    # Balloon outlines cut by a tile/crop edge tend to come back as a lone "_" or "|".
    text = " ".join(t[2].strip() for t in lines if t[2].strip() not in ("", "_", "|"))
    text = " ".join(tok for tok in text.split() if tok not in ("_", "|"))
    conf = float(np.mean([t[3] for t in lines])) if lines else None
    return text, conf


def ocr_doubts(text, conf, src_lang, capacity=None) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["empty"]
    compact = re.sub(r"\s", "", text)
    if _PUNCT_ONLY.fullmatch(compact):
        # "…!?" balloons are legitimate; only a large text box that yields nothing else is doubtful.
        return ["too_short"] if capacity and capacity >= 4 else []
    letters = (_JA_LETTER if src_lang == "ja" else _EN_LETTER).findall(compact)
    reasons = []
    if not letters:
        reasons.append("no_letters")
    if len(compact) >= 4 and max(compact.count(c) for c in set(compact)) / len(compact) >= 0.7:
        reasons.append("repeated")
    if src_lang != "ja" and conf is not None and conf < 0.35:
        reasons.append("low_confidence")
    if capacity and capacity >= 4 and len(letters) < capacity * 0.25:
        reasons.append("too_short")
    return reasons


def reading_score(text, conf, src_lang, doubts) -> float:
    letters = len((_JA_LETTER if src_lang == "ja" else _EN_LETTER).findall(text or ""))
    return letters * (conf if conf is not None else 1.0) - 5 * len(doubts)


def retry_variants(crop: Image.Image) -> list[Image.Image]:
    """Padded + contrast-stretched (upscaled when small), then a binarized copy."""
    grey = ImageOps.autocontrast(crop.convert("L"), cutoff=2)
    arr = np.asarray(grey)
    edge = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    pad = max(8, round(min(crop.size) * 0.15))
    padded = ImageOps.expand(grey, border=pad, fill=int(np.median(edge)))
    if min(crop.size) < 64:
        padded = padded.resize((padded.width * 2, padded.height * 2), Image.LANCZOS)
    _, binary = cv2.threshold(np.asarray(padded), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return [padded.convert("RGB"), Image.fromarray(binary).convert("RGB")]


# ---------- engines ----------

def group_crop(img: Image.Image, rects: list[Rect]) -> Image.Image:
    box = union_rect(rects)
    size = median(min(r[2] - r[0], r[3] - r[1]) for r in rects)
    pad = max(3, int(size * 0.15))
    return img.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                     min(img.width, box[2] + pad), min(img.height, box[3] + pad)))


def visible_first(order: list[int], boxes: list[Rect], band) -> list[int]:
    """The reading order, with the boxes that touch the band (top, bottom in pixels) first. Each group keeps its reading order.
    No band keeps the order as it is."""
    if band is None:
        return order
    top, bottom = band
    inside = [i for i in order if boxes[i][1] < bottom and boxes[i][3] > top]
    inside_set = set(inside)
    return inside + [i for i in order if i not in inside_set]


def clean_rects(rects: list[Rect], width: int, height: int) -> list[Rect]:
    """Clips the detected boxes to the image, drops specks, and merges the same line seen by two overlapping tiles."""
    clipped = [[max(0, r[0]), max(0, r[1]), min(width, r[2]), min(height, r[3])] for r in rects]
    return dedupe_rects([r for r in clipped if r[2] - r[0] >= 4 and r[3] - r[1] >= 4])


def spans_of(rects: list[Rect]) -> tuple[list, int]:
    """The dialogue regions of a page: boxes grouped by distance, each as (member boxes, union box). Returns the regions and the
    number of groups; a region too small to hold text is dropped."""
    groups = group_boxes(rects)
    spans = []
    for g in groups:
        members = [rects[i] for i in g]
        box = union_rect(members)
        if max(box[2] - box[0], box[3] - box[1]) < 12:
            continue
        spans.append((members, box))
    return spans, len(groups)


def in_reading_order(spans: list, reading: str, band) -> list[int]:
    """Indices of the regions in reading order, the ones that touch the band (top, bottom in pixels) first."""
    boxes = [box for _, box in spans]
    return visible_first(order_rects(boxes, reading), boxes, band)


def tile_order(ranges: list[tuple[int, int]], band) -> list[int]:
    """The order tiles are detected in: the tiles that touch the band (top, bottom in pixels) first, then the rest in page order."""
    if band is None:
        return list(range(len(ranges)))
    top, bottom = band
    near = [i for i, (y0, y1) in enumerate(ranges) if y0 < bottom and y1 > top]
    return near + [i for i in range(len(ranges)) if i not in near]


def group_reach(rects: list[Rect]) -> float:
    """How far from a region a box that is not detected yet can lie and still join it. Two boxes join when their gap is below the
    sum of their pads, and a pad is at most 0.3 x the box side, so 0.6 x the largest side seen is the reach."""
    return 0.6 * max((min(r[2] - r[0], r[3] - r[1]) for r in rects), default=0) + 4


def touches_pending(box: Rect, pending: list[tuple[int, int]], reach: float) -> bool:
    """True when a tile that is not detected yet lies within `reach` of the region (so it may still change)."""
    return any(y0 < box[3] + reach and y1 > box[1] - reach for y0, y1 in pending)


class PlanStep(NamedTuple):
    """One step of plan_steps(). `spans` are the regions to read now, in reading order. The last step has final=True and also
    carries the page: all_spans (every region, in group order), order (indices into all_spans in reading order, the band first),
    and stats."""
    spans: list
    final: bool = False
    all_spans: list | None = None
    order: list | None = None
    stats: dict | None = None


class OcrEngine:
    def __init__(self, gpu: bool = True):
        self.gpu = gpu
        self._reader = None
        self._manga = None
        self._primed = False
        self._init_lock = threading.Lock()  # warmup thread and worker may both ask for a model
        # One inference at a time: the warm-up pass, a page read and a region read share the GPU. Re-entrant, because run() holds it
        # while it plans and reads.
        self._infer_lock = threading.RLock()

    @property
    def reader(self):
        with self._init_lock:
            if self._reader is None:
                import easyocr
                self._reader = easyocr.Reader(["en"], gpu=self.gpu, verbose=False)
            return self._reader

    @property
    def manga(self):
        with self._init_lock:
            if self._manga is None:
                from manga_ocr import MangaOcr
                self._manga = MangaOcr(force_cpu=not self.gpu)
            return self._manga

    @property
    def ready(self) -> bool:
        """Both models are loaded and the GPU has run each of them once, so the next page does not pay for the first inference."""
        return self._reader is not None and self._manga is not None and self._primed

    def warmup(self, src_langs=("ja", "en")):
        _ = self.reader
        if "ja" in src_langs:
            _ = self.manga
        self.prime(src_langs)

    def prime(self, src_langs=("ja", "en")):
        """The first inference on a GPU is slow (its kernels are set up then): measured 26 s for the first page after
        a restart, against about 1 s after that. Run one small input through each model now, so the first real page
        does not pay for it. A pass that fails still ends the warm-up: the first page then loads what is missing."""
        with self._infer_lock:
            try:
                blank = Image.new("RGB", (320, 320), "white")
                self.detect(blank, "ja")  # the text detector's network runs even though a blank page has no boxes
                if "ja" in src_langs:
                    self.manga(Image.new("RGB", (96, 96), "white"))
                if "en" in src_langs:
                    self.reader.readtext(np.asarray(Image.new("RGB", (96, 96), "white")), detail=0)
            finally:
                self._primed = True

    def _detect_tile(self, arr: np.ndarray, tile: tuple[int, int], src_lang: str) -> list[Rect]:
        """The text boxes of one vertical tile of the page, in page coordinates."""
        y0, y1 = tile
        horizontal, free = self.reader.detect(
            arr[y0:y1],
            min_size=10,
            text_threshold=0.5,
            low_text=0.3,
            link_threshold=0.3,
            # Japanese columns shouldn't be glued across balloons; grouping merges them later.
            width_ths=0.1 if src_lang == "ja" else 0.5,
            add_margin=0.05,
        )
        rects = [[int(x_min), int(b_min) + y0, int(x_max), int(b_max) + y0] for x_min, x_max, b_min, b_max in horizontal[0]]
        for poly in free[0]:
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            rects.append([int(min(xs)), int(min(ys)) + y0, int(max(xs)), int(max(ys)) + y0])
        return rects

    def detect(self, img: Image.Image, src_lang: str) -> list[Rect]:
        arr = np.asarray(img)
        rects: list[Rect] = []
        for tile in tile_ranges(img.width, img.height):
            rects += self._detect_tile(arr, tile, src_lang)
        return clean_rects(rects, img.width, img.height)

    def manga_read(self, crops: list[Image.Image]) -> list[str]:
        """Japanese reading of many crops, a few per generate call (about 3x faster than one call per crop).
        The cap keeps a long webtoon page with many balloons from making one huge GPU batch."""
        out = []
        for i in range(0, len(crops), MANGA_BATCH):
            out += self._manga_batch(crops[i:i + MANGA_BATCH])
        return out

    def _manga_batch(self, crops: list[Image.Image]) -> list[str]:
        from manga_ocr.ocr import post_process
        import torch

        m = self.manga
        x = torch.stack([m._preprocess(c.convert("L").convert("RGB")) for c in crops]).to(m.model.device)
        out = m.model.generate(x, max_length=300).cpu()
        return [post_process(t) for t in m.tokenizer.batch_decode(out, skip_special_tokens=True)]

    def _recognize_groups(self, grey: np.ndarray, groups: list[list[Rect]]) -> list[tuple[str, float | None]]:
        """English reading of every group in one recognizer call. Each line comes back with its box, which names its group."""
        owner, flat = {}, []
        for gi, members in enumerate(groups):
            for r in members:
                owner[tuple(r)] = gi
                flat.append([r[0], r[2], r[1], r[3]])
        lines = [[] for _ in groups]
        for pts, text, conf in self.reader.recognize(grey, horizontal_list=flat, free_list=[], detail=1):
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            gi = owner.get((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
            if gi is not None:
                lines[gi].append((pts, text, conf))
        return [_join_lines(group) for group in lines]

    def _read_crop(self, crop: Image.Image, src_lang: str):
        if src_lang == "ja":
            return self.manga(crop).strip(), None
        return _join_lines(self.reader.readtext(np.asarray(crop), detail=1, paragraph=False))

    def _verify_all(self, crops, firsts, capacities, src_lang, reread, stats=None):
        """Re-reads only the doubtful crops, with padding / contrast / binarization, and keeps the best reading of each.
        All the crops still in doubt go through the same variant together, so the Japanese model reads them as one batch.
        `firsts` are the first readings (text, conf); `reread(variants)` reads the variants. Returns (text, conf, doubts, retries)
        per crop. `stats`, when given, adds up retry_spans / retry_fixed / retry_ms."""
        best = []
        for (text, conf), capacity in zip(firsts, capacities):
            doubts = ocr_doubts(text, conf, src_lang, capacity)
            best.append((reading_score(text, conf, src_lang, doubts), text, conf, doubts))
        retries = [0] * len(crops)
        pending = [i for i, entry in enumerate(best) if entry[3]]
        started = list(pending)
        t0 = time.perf_counter()
        variants = {i: retry_variants(crops[i]) for i in pending}
        for round_no in range(max((len(v) for v in variants.values()), default=0)):
            if not pending:
                break
            still = []
            for i, (text, conf) in zip(pending, reread([variants[i][round_no] for i in pending])):
                retries[i] += 1
                doubts = ocr_doubts(text, conf, src_lang, capacities[i])
                score = reading_score(text, conf, src_lang, doubts)
                if score > best[i][0]:
                    best[i] = (score, text, conf, doubts)
                if doubts:
                    still.append(i)
            pending = still
        if stats is not None and started:
            stats["retry_spans"] = stats.get("retry_spans", 0) + len(started)
            stats["retry_fixed"] = stats.get("retry_fixed", 0) + sum(1 for i in started if not best[i][3])
            stats["retry_ms"] = stats.get("retry_ms", 0) + round((time.perf_counter() - t0) * 1000)
        return [(entry[1], entry[2], entry[3], n) for entry, n in zip(best, retries)]

    def _reread_each(self, src_lang: str):
        """A `reread` that reads the variants one by one (English, and the single crop of a region)."""
        return lambda variants: [self._read_crop(v, src_lang) for v in variants]

    def read_region(self, img: Image.Image, bbox, src_lang: str):
        """OCR one reader-drawn region. Returns (text, conf, uncertain)."""
        with self._infer_lock:
            return self._read_region(img, bbox, src_lang)

    def _read_region(self, img: Image.Image, bbox, src_lang: str):
        x, y, w, h = bbox
        pad = max(3, int(min(w, h) * 0.1))
        crop = img.crop((max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad)))
        capacity = max(w, h) / max(1, min(w, h)) if src_lang == "ja" else None
        [(text, conf, doubts, _)] = self._verify_all([crop], [self._read_crop(crop, src_lang)], [capacity], src_lang,
                                                     self._reread_each(src_lang))
        return text, conf, bool(doubts)

    def run(self, img: Image.Image, src_lang: str, reading: str):
        """Returns (segments, timings). Segment bbox is [x, y, w, h] in image pixels."""
        with self._infer_lock:
            return self._run(img, src_lang, reading)

    def plan(self, img: Image.Image, src_lang: str, reading: str, visible=None):
        """Detection and grouping only (GPU). Returns (spans, order, stats): spans are (member rects, union box) in group order, and
        order lists their indices in reading order. With `visible` = (top, bottom) in pixels, the spans that touch that band come
        first (still in reading order), so a long page shows the part the reader is looking at before the rest."""
        for step in self.plan_steps(img, src_lang, reading, visible):
            if step.final:
                return step.all_spans, step.order, step.stats

    def plan_steps(self, img: Image.Image, src_lang: str, reading: str, visible=None):
        """plan() in steps. A page of several tiles is detected tile by tile, the tiles at `visible` first. A step carries the regions
        whose surroundings are detected already, so reading can start before the rest of the page is detected; the last step carries
        the regions not yielded before, and the page itself. A region yielded early can still turn out to be part of a larger one
        when a later tile adds a box next to it: the last step then lists the larger region, which is read as new."""
        tiles = tile_ranges(img.width, img.height)
        detect_ms, found, sent = 0.0, {}, set()
        if len(tiles) == 1:
            t0 = time.perf_counter()
            with self._infer_lock:
                rects = self.detect(img, src_lang)
            yield self._last_step(rects, reading, visible, sent, 1, (time.perf_counter() - t0) * 1000)
            return
        arr = np.asarray(img)
        for k in tile_order(tiles, visible):
            t0 = time.perf_counter()
            with self._infer_lock:
                found[k] = self._detect_tile(arr, tiles[k], src_lang)
            detect_ms += (time.perf_counter() - t0) * 1000
            waiting = [tiles[j] for j in range(len(tiles)) if j not in found]
            if not waiting:
                break
            rects = clean_rects([r for j in sorted(found) for r in found[j]], img.width, img.height)
            spans, _ = spans_of(rects)
            reach = group_reach(rects)
            ready = [s for s in spans if tuple(s[1]) not in sent and not touches_pending(s[1], waiting, reach)]
            if ready:
                first = [ready[i] for i in in_reading_order(ready, reading, visible)]
                sent.update(tuple(box) for _, box in first)
                yield PlanStep(first)
        rects = clean_rects([r for j in sorted(found) for r in found[j]], img.width, img.height)
        yield self._last_step(rects, reading, visible, sent, len(tiles), detect_ms)

    @staticmethod
    def _last_step(rects, reading, visible, sent, tiles, detect_ms):
        spans, groups = spans_of(rects)
        order = in_reading_order(spans, reading, visible)
        stats = {"boxes": len(rects), "groups": groups, "tiles": tiles, "detect_ms": round(detect_ms)}
        return PlanStep([spans[i] for i in order if tuple(spans[i][1]) not in sent], True, spans, order, stats)

    def read_spans(self, img: Image.Image, spans, src_lang: str, stats=None):
        """Recognition for the given spans (GPU). Returns one (box, text, conf, doubts, retries) per span, in the same order.
        The spans are read together (Japanese in batches), and so are the re-reads of the doubtful ones. `stats`, when given, adds
        up retry_spans / retry_fixed / retry_ms."""
        with self._infer_lock:
            return self._read_spans(img, spans, src_lang, stats)

    def _read_spans(self, img: Image.Image, spans, src_lang: str, stats=None):
        if not spans:
            return []
        crops = [group_crop(img, members) for members, _ in spans]
        if src_lang == "ja":
            firsts = [(text.strip(), None) for text in self.manga_read(crops)]
            # Vertical columns: about one character per column width of height.
            capacities = [sum(max(r[2] - r[0], r[3] - r[1]) / max(1, min(r[2] - r[0], r[3] - r[1])) for r in members)
                          for members, _ in spans]

            def reread(variants):
                return [(text.strip(), None) for text in self.manga_read(variants)]
        else:
            firsts = self._recognize_groups(np.asarray(img.convert("L")), [members for members, _ in spans])
            capacities = [None] * len(spans)
            reread = self._reread_each(src_lang)
        checked = self._verify_all(crops, firsts, capacities, src_lang, reread, stats)
        self._primed = True
        return [(box, text, conf, doubts, retries) for (_, box), (text, conf, doubts, retries) in zip(spans, checked)]

    def _run(self, img: Image.Image, src_lang: str, reading: str):
        t0 = time.perf_counter()
        spans, _, stats = self.plan(img, src_lang, reading)
        t1 = time.perf_counter()
        read = self.read_spans(img, spans, src_lang)
        t2 = time.perf_counter()
        candidates = [(box, text, conf, doubts) for box, text, conf, doubts, _ in read if text]
        retries = sum(item[4] for item in read)

        order = order_rects([c[0] for c in candidates], reading)
        segments = []
        for new_id, idx in enumerate(order):
            box, text, conf, doubts = candidates[idx]
            seg = {
                "id": new_id,
                "bbox": [box[0], box[1], box[2] - box[0], box[3] - box[1]],
                "src": text,
                "conf": conf,
            }
            if doubts:
                seg.update(ocr_uncertain=True, ocr_doubts=doubts)
            segments.append(seg)
        timings = {"detect_ms": round((t1 - t0) * 1000), "recognize_ms": round((t2 - t1) * 1000),
                   "boxes": stats["boxes"], "groups": stats["groups"], "ocr_retries": retries}
        return segments, timings
