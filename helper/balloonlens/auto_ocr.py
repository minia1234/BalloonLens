"""Select local OCR readings by script and confidence; translation detects the language of the resulting text.

Optional EasyOCR recognizers are used only when their weights already exist. No model download is started here.
"""
import os
import re
from pathlib import Path

import numpy as np

from .backends.base import BackendError
from .ocr import group_crop

PROFILES = {
    "ko": ("korean_g2.pth", "ko", r"[가-힣ㄱ-ㆎ]"),
    "zh-Hans": ("zh_sim_g2.pth", "ch_sim", r"[一-鿿]"),
    "zh-Hant": ("chinese.pth", "ch_tra", r"[一-鿿]"),
    "latin": ("latin_g2.pth", "fr", r"[A-Za-zÀ-ž]"),
    "cyrillic": ("cyrillic_g2.pth", "ru", r"[Ѐ-ԯ]"),
    "arabic": ("arabic.pth", "ar", r"[؀-ۿ]"),
    "devanagari": ("devanagari.pth", "hi", r"[ऀ-ॿ]"),
    "bengali": ("bengali.pth", "bn", r"[ঀ-৿]"),
    "thai": ("thai.pth", "th", r"[฀-๿]"),
}


def model_folder():
    return Path(os.environ.get("EASYOCR_MODULE_PATH") or os.environ.get("MODULE_PATH") or Path.home() / ".EasyOCR") / "model"


class AutoOCR:
    def __init__(self, engine):
        self.engine = engine
        self.readers = {}

    @property
    def available(self):
        return ["ja", "en", *[name for name, (file, _, _) in PROFILES.items() if (model_folder() / file).is_file()]]

    @property
    def signature(self):
        return "auto-v2:" + ",".join(self.available)

    def reader(self, language):
        if language == "en":
            return self.engine.reader
        file, code, _ = PROFILES[language]
        if not (model_folder() / file).is_file():
            raise BackendError("ocr_model_missing", language)
        if language not in self.readers:
            import easyocr
            self.readers[language] = easyocr.Reader([code, "en"], gpu=self.engine.gpu, detector=False,
                                                   model_storage_directory=str(model_folder()),
                                                   download_enabled=False, verbose=False)
        return self.readers[language]

    @staticmethod
    def score(text, confidence, script):
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return 0.1 if text.strip() else -1
        matches = len(re.findall(script, text))
        if not matches:
            return -1
        coverage = matches / len(letters)
        return coverage * (0.8 + float(confidence or 0)) + min(matches, 12) * 0.01

    def read_spans(self, image, spans, stats=None):
        if not spans:
            return []
        grey = np.asarray(image.convert("L"))
        groups = [members for members, _ in spans]
        first = self.engine._recognize_groups(grey, groups)
        best = [(self.score(text, conf, r"[A-Za-zÀ-ž]"), text, conf, "en") for text, conf in first]
        vertical = {i for i, (_, box) in enumerate(spans)
                    if box[3] - box[1] > 1.6 * max(1, box[2] - box[0])}
        settled = {i for i, (score, text, conf, _) in enumerate(best)
                   if i not in vertical and conf is not None and conf >= 0.85 and score >= 1.65
                   and len([c for c in text if c.isalpha()]) >= 3}
        manga_seen = set()

        def read_japanese(indices):
            crops = [group_crop(image, spans[i][0]) for i in indices]
            for i, text in zip(indices, self.engine.manga_read(crops)):
                manga_seen.add(i)
                kana = len(re.findall(r"[぀-ヿ]", text))
                han = len(re.findall(r"[一-鿿]", text))
                score = (1.45 if kana else 0.85 if han else -1) + min(kana + han, 12) * 0.01
                if score > best[i][0]:
                    best[i] = score, text.strip(), None, "ja"
                    if kana >= 2:
                        settled.add(i)

        # Vertical Japanese dialogue can use Manga OCR directly instead of visiting every installed recognizer.
        if vertical:
            read_japanese(sorted(vertical))
        for language in self.available:
            if language in {"ja", "en"}:
                continue
            indices = [i for i in range(len(spans)) if i not in settled]
            if not indices:
                break
            reader = self.reader(language)
            readings = self.engine._recognize_groups(grey, [groups[i] for i in indices], reader=reader)
            for i, (text, conf) in zip(indices, readings):
                score = self.score(text, conf, PROFILES[language][2])
                if score > best[i][0]:
                    best[i] = score, text, conf, language
                    if conf is not None and conf >= 0.85 and score >= 1.65:
                        settled.add(i)
        # Confident Latin readings need no Japanese re-read. Other scripts compete with Manga OCR locally.
        pending = [i for i, (_score, text, conf, script) in enumerate(best)
                   if i not in settled and i not in manga_seen
                   and (not conf or conf < (0.85 if script in {"en", "latin"} else 0.7))]
        if pending:
            read_japanese(pending)
        output = [None] * len(spans)
        scripts = stats.setdefault("auto_scripts", []) if stats is not None else []
        for script in dict.fromkeys(item[3] for item in best):
            if script not in scripts:
                scripts.append(script)
            indices = [i for i, item in enumerate(best) if item[3] == script]
            crops = [group_crop(image, spans[i][0]) for i in indices]
            firsts = [(best[i][1], best[i][2]) for i in indices]
            capacities = [sum(max(r[2] - r[0], r[3] - r[1]) / max(1, min(r[2] - r[0], r[3] - r[1]))
                              for r in spans[i][0]) if script == "ja" else None for i in indices]
            if script == "ja":
                def reread(variants):
                    return [(text.strip(), None) for text in self.engine.manga_read(variants)]
            else:
                reread = self.engine._reread_each(script)
            checked = self.engine._verify_all(crops, firsts, capacities, script, reread, stats)
            for i, (text, conf, doubts, retries) in zip(indices, checked):
                output[i] = (spans[i][1], text, conf, doubts, retries)
        return output
