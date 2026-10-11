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

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import config

Rect = list  # [x0, y0, x1, y1] in image pixels


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

class OcrEngine:
    def __init__(self, gpu: bool = True):
        self.gpu = gpu
        self._reader = None
        self._manga = None
        self._init_lock = threading.Lock()  # warmup thread and worker may both ask for a model

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
        return self._reader is not None and self._manga is not None

    def warmup(self, src_langs=("ja", "en")):
        _ = self.reader
        if "ja" in src_langs:
            _ = self.manga

    def detect(self, img: Image.Image, src_lang: str) -> list[Rect]:
        arr = np.asarray(img)
        rects: list[Rect] = []
        for y0, y1 in tile_ranges(img.width, img.height):
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
            for x_min, x_max, b_min, b_max in horizontal[0]:
                rects.append([int(x_min), int(b_min) + y0, int(x_max), int(b_max) + y0])
            for poly in free[0]:
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                rects.append([int(min(xs)), int(min(ys)) + y0, int(max(xs)), int(max(ys)) + y0])
        w, h = img.width, img.height
        clipped = [[max(0, r[0]), max(0, r[1]), min(w, r[2]), min(h, r[3])] for r in rects]
        return dedupe_rects([r for r in clipped if r[2] - r[0] >= 4 and r[3] - r[1] >= 4])

    def recognize_group(self, img: Image.Image, grey: np.ndarray, rects: list[Rect], src_lang: str):
        """Returns (text, conf, info); info = {"doubts": [...], "retries": n}."""
        box = union_rect(rects)
        size = median(min(r[2] - r[0], r[3] - r[1]) for r in rects)
        pad = max(3, int(size * 0.15))
        crop = img.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                         min(img.width, box[2] + pad), min(img.height, box[3] + pad)))
        if src_lang == "ja":
            text, conf = self.manga(crop).strip(), None
            # Vertical columns: about one character per column width of height.
            capacity = sum(max(r[2] - r[0], r[3] - r[1]) / max(1, min(r[2] - r[0], r[3] - r[1])) for r in rects)
        else:
            results = self.reader.recognize(
                grey, horizontal_list=[[r[0], r[2], r[1], r[3]] for r in rects], free_list=[], detail=1
            )
            text, conf = _join_lines(results)
            capacity = None
        return self._verify(crop, text, conf, src_lang, capacity)

    def _read_crop(self, crop: Image.Image, src_lang: str):
        if src_lang == "ja":
            return self.manga(crop).strip(), None
        return _join_lines(self.reader.readtext(np.asarray(crop), detail=1, paragraph=False))

    def _verify(self, crop, text, conf, src_lang, capacity):
        """Re-read only a doubtful crop with padding / contrast / binarization and keep the best reading."""
        doubts = ocr_doubts(text, conf, src_lang, capacity)
        retries = 0
        if doubts:
            best = (reading_score(text, conf, src_lang, doubts), text, conf, doubts)
            for variant in retry_variants(crop):
                retries += 1
                t, c = self._read_crop(variant, src_lang)
                d = ocr_doubts(t, c, src_lang, capacity)
                score = reading_score(t, c, src_lang, d)
                if score > best[0]:
                    best = (score, t, c, d)
                if not d:
                    break
            _, text, conf, doubts = best
        return text, conf, {"doubts": doubts, "retries": retries}

    def read_region(self, img: Image.Image, bbox, src_lang: str):
        """OCR one reader-drawn region. Returns (text, conf, uncertain)."""
        x, y, w, h = bbox
        pad = max(3, int(min(w, h) * 0.1))
        crop = img.crop((max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad)))
        text, conf = self._read_crop(crop, src_lang)
        capacity = max(w, h) / max(1, min(w, h)) if src_lang == "ja" else None
        text, conf, info = self._verify(crop, text, conf, src_lang, capacity)
        return text, conf, bool(info["doubts"])

    def run(self, img: Image.Image, src_lang: str, reading: str):
        """Returns (segments, timings). Segment bbox is [x, y, w, h] in image pixels."""
        t0 = time.perf_counter()
        rects = self.detect(img, src_lang)
        groups = group_boxes(rects)
        t1 = time.perf_counter()

        grey = np.asarray(img.convert("L")) if src_lang != "ja" else None
        candidates, retries = [], 0
        for g in groups:
            members = [rects[i] for i in g]
            box = union_rect(members)
            if max(box[2] - box[0], box[3] - box[1]) < 12:
                continue
            text, conf, info = self.recognize_group(img, grey, members, src_lang)
            retries += info["retries"]
            if text:
                candidates.append((box, text, conf, info["doubts"]))
        t2 = time.perf_counter()

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
                   "boxes": len(rects), "groups": len(groups), "ocr_retries": retries}
        return segments, timings
