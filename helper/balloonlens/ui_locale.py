"""Bundled interface languages. This module never calls a model or uses account credentials."""
import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[2] / "extension" / "ui_catalog.json"
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
PACKS = json.loads(CATALOG_PATH.with_name("ui_languages.json").read_text(encoding="utf-8"))
LANGUAGE_CHOICES = [f"{label} ({code})" for code, label in PACKS["languages"].items()]


def language_key(value):
    value = str(value or "ko").strip()
    match = re.search(r"\(([A-Za-z0-9-]+)\)$", value)
    if match:
        value = match[1]
    value = {"한국어": "ko", "Korean": "ko", "English": "en", "日本語": "ja", "Japanese": "ja"}.get(value, value)
    if not value or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_ui_language")
    lower = value.lower()
    return next((code for code, label in PACKS["languages"].items() if code.lower() == lower or label.lower() == lower),
                PACKS["aliases"].get(lower, lower.split("-")[0]))


def builtin(language):
    if language == "ko":
        return {}
    if language not in PACKS["languages"]:
        raise ValueError("ui_language_not_bundled")
    index = 1 if language == "ja" else 0
    translated = {source: values[index] for source, values in CATALOG.items()}
    if language in PACKS["order"]:
        column = PACKS["order"].index(language)
        for source, values in PACKS["strings"].items():
            translated[source] = values[column]
    return translated


class UITranslator:
    def __init__(self, language="ko"):
        self.set(language)

    def set(self, language):
        language = language_key(language)
        translated = builtin(language)
        self.language = language
        self.translations = translated
        self.patterns = []
        for source in sorted(self.translations, key=len, reverse=True):
            names = re.findall(r"\{(\w+)\}", source)
            if not names:
                continue
            pattern = re.escape(source)
            for name in names:
                capture = r"[-\d.,]+" if name in {"count", "width", "height", "x", "y", "shown", "total"} else r".+?"
                pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name}>{capture})")
            self.patterns.append((re.compile("^" + pattern + "$", re.S), self.translations[source]))

    def text(self, source, depth=0):
        source = str(source)
        if self.language == "ko" or depth > 3:
            return source
        if source in self.translations:
            return self.translations[source]
        for pattern, target in self.patterns:
            match = pattern.fullmatch(source)
            if match:
                values = {k: self.text(v, depth + 1) for k, v in match.groupdict().items()}
                return re.sub(r"\{(\w+)\}", lambda item: values.get(item[1], item[0]), target)
        if "\n" in source:
            return "\n".join(self.text(part, depth + 1) for part in source.split("\n"))
        if " · " in source:
            return " · ".join(self.text(part, depth + 1) for part in source.split(" · "))
        return source

    def apply_widgets(self, widget):
        # Only control labels/buttons: input fields, source dialogue and transcript contents keep their text.
        if widget.winfo_class() in {"TLabel", "Label", "TButton", "Button", "TCheckbutton", "Checkbutton", "TLabelframe"}:
            variable = str(widget.cget("textvariable")) if "textvariable" in widget.keys() else ""
            current = widget.getvar(variable) if variable else widget.cget("text")
            source, last = getattr(widget, "_ui_text", (current, None))
            if current != last:
                source = current
            translated = self.text(source)
            if current != translated:
                widget.setvar(variable, translated) if variable else widget.configure(text=translated)
            widget._ui_text = (source, translated)
        for child in widget.winfo_children():
            self.apply_widgets(child)


