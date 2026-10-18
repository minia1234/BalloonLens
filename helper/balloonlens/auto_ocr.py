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
        return "auto-v1:" + ",".join(self.available)

    def reader(self, language):
        if language == "en":
            return self.engine.reader
        file, code, _ = PROFILES[language]
        if not (model_folder() / file).is_file():
            raise BackendError("ocr_model_missing", language)
        if language not in self.readers:
            import easyocr
            self.readers[language] = easyocr.Reader([code, "en"], gpu=False, detector=False,
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
        for language in self.available:
            if language in {"ja", "en"}:
                continue
            reader = self.reader(language)
            for i, (text, conf) in enumerate(self.engine._recognize_groups(grey, groups, reader=reader)):
                score = self.score(text, conf, PROFILES[language][2])
                if score > best[i][0]:
                    best[i] = score, text, conf, language
        # Confident Latin readings need no Japanese re-read. Other scripts compete with Manga OCR locally.
        pending = [i for i, (_score, text, conf, script) in enumerate(best)
                   if not conf or conf < (0.85 if script in {"en", "latin"} else 0.7)
                   or spans[i][1][3] - spans[i][1][1] > 1.6 * max(1, spans[i][1][2] - spans[i][1][0])]
        if pending:
            crops = [group_crop(image, spans[i][0]) for i in pending]
            for i, text in zip(pending, self.engine.manga_read(crops)):
                kana = len(re.findall(r"[぀-ヿ]", text))
                han = len(re.findall(r"[一-鿿]", text))
                score = (1.45 if kana else 0.85 if han else -1) + min(kana + han, 12) * 0.01
                if score > best[i][0]:
                    best[i] = score, text.strip(), None, "ja"
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
