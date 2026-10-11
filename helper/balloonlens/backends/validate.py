"""Output checks shared by every translation path.

These are heuristics. They catch the wrong script, lost or invented numbers, leaked secrets, stray
meta text and words the page never contained. They do not prove a translation is faithful; that
needs a human reader (see the blind evaluation in tools/blind_eval.py).
"""
import re
import unicodedata
from collections import Counter

_THINK = re.compile(r"<think>.*?</think>", re.S)


def clean_output(text: str) -> str:
    text = _THINK.sub("", text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _script_ratio(text: str, pattern: str) -> float | None:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    return sum(1 for c in letters if re.match(pattern, c)) / len(letters)


def looks_like_target(text: str, tgt: str) -> bool:
    """Cheap wrong-output-language check. Lines with no letters (e.g. '...!?') pass."""
    patterns = {"ko": r"[가-힣ㄱ-ㆎ]", "ja": r"[぀-ヿ一-鿿]",
                "en": r"[A-Za-z]", "zh-Hans": r"[一-鿿]", "zh-Hant": r"[一-鿿]"}
    if tgt not in patterns:
        return True
    ratio = _script_ratio(text, patterns[tgt])
    return ratio is None or ratio >= 0.5


def _page_quality(source, output, target):
    if not output:
        return ["missing_or_empty"]
    reasons = []
    if not looks_like_target(output, target):
        reasons.append("target_script")
    # A line with letters must not come back as symbols only (the target-language check lets symbol-only text pass).
    if any(c.isalpha() for c in source) and not any(c.isalpha() for c in output):
        reasons.append("no_letters")
    numbers = lambda text: Counter(re.findall(r"\d+(?:[.,]\d+)*", unicodedata.normalize("NFKC", text)))
    if numbers(source) - numbers(output):
        reasons.append("number_omission")
    # Written source numbers can legitimately become Arabic digits in Korean.
    written = Counter()
    english = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
    for word in re.findall(r'\b[a-z]+\b', source.lower()):
        if word in english:
            written[str(english.index(word))] += 1
    digits = dict(zip('零〇一二三四五六七八九', (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
    units = {'十': 10, '百': 100, '千': 1000}
    for word in re.findall(r'[零〇一二三四五六七八九十百千]+', source):
        value, digit = 0, 0
        if not any(c in units for c in word):
            value = int(''.join(str(digits[c]) for c in word))
        else:
            for c in word:
                if c in digits:
                    digit = digits[c]
                else:
                    value, digit = value + (digit or 1) * units[c], 0
            value += digit
        written[str(value)] += 1
    if numbers(output) - numbers(source) - written:
        reasons.append("number_addition")
    return reasons


def glossary_terms(glossary: str) -> list[tuple[str, str]]:
    """'田中 = 다나카' style lines (also ->, →, :) as (source, target) pairs."""
    pairs = []
    for line in str(glossary or "").splitlines():
        m = re.match(r"\s*(.+?)\s*(?:=|->|→|:)\s*(.+?)\s*$", line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs


_NUMBERED = re.compile(r"\d|[一二三四五六七八九十百千]+[時分人回個日月年歳円本枚]|\b(?:one|two|three|four|five|six|seven|"
                       r"eight|nine|ten|eleven|twelve|twenty|thirty|hundred|thousand)\b", re.I)
_VOLITIONAL = re.compile(r"(?:ましょう|[おこそとのほもよろごぞどぼぽ]う|よう)[よね]?[！？!?。…～〜]*$|^let'?s\b", re.I)
_KATAKANA_NAME = re.compile(r"[ァ-ヺ]{3,}")
_EN_NAME = re.compile(r"\s[A-Z][a-z]{2,}")
_POLITE_END = re.compile(r"(?:요|습니다|습니까|세요)[.!?…~]*$")


def review_reasons(source, draft, *, terms=(), casual=False, uncertain=False, flags=()) -> list[str]:
    """Concrete risks that justify a proofreading pass. Absence is not proof the draft is right."""
    reasons = list(flags)
    if uncertain:
        reasons.append("ocr_uncertain")
    if draft is None:
        reasons.append("missing")
    text = unicodedata.normalize("NFKC", source).strip()
    if _NUMBERED.search(text):
        reasons.append("number")
    for term, target in terms:
        if term and term in source:
            reasons.append("glossary_term")
            if draft and target not in draft:
                reasons.append("glossary_conflict")
    if _KATAKANA_NAME.search(source) or _EN_NAME.search(text):
        reasons.append("name")
    if _VOLITIONAL.search(text):
        reasons.append("speech_act")
    if draft and re.search(r"[?？]", source) and not re.search(r"[?？]", draft):
        reasons.append("question_lost")
    if casual and draft and _POLITE_END.search(draft.strip()):
        reasons.append("style_mismatch")
    return list(dict.fromkeys(reasons))


# ---- checks specific to tool-capable models (added in v0.5.0) ----

_META = re.compile(r"^(?:다음은|번역\s*[:：]|번역문\s*[:：]|translation\s*[:：]|here is|here's|sure[,!]|"
                   r"i can(?:not|'t)|as an ai|note\s*[:：])", re.I | re.M)
_SECRET = [re.compile(p) for p in (
    r"sk-ant-[A-Za-z0-9_\-]{10,}",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"auth\.json",
    r"\.credentials\.json",
    r"(?i)api[_-]?key\s*[:=]\s*\S{8,}",
)]
_URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)
_WINPATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|]+")
_COMMAND = re.compile(r"\b(?:powershell|pwsh|cmd\.exe|rm\s+-rf|curl\s|wget\s|Invoke-WebRequest|"
                      r"Invoke-Expression|del\s+/[fq])\b", re.I)


def meta_speech(text: str) -> bool:
    return bool(text and _META.search(text))


def secret_hits(text: str) -> list[str]:
    """Names of matched patterns only; the matched text itself is never returned or logged."""
    return [p.pattern for p in _SECRET if text and p.search(text)]


def injection_reasons(source: str, text: str) -> list[str]:
    """Things the reply contains that the source line did not: links, Windows paths, shell commands."""
    source = source or ""
    reasons = []
    if any(url.lower() not in source.lower() for url in _URL.findall(text or "")):
        reasons.append("foreign_url")
    if any(path not in source for path in _WINPATH.findall(text or "")):
        reasons.append("foreign_path")
    if _COMMAND.search(text or "") and not _COMMAND.search(source):
        reasons.append("command_text")
    return reasons


def page_reasons(source, text, tgt) -> list[str]:
    """All automatic checks for one translated line, deduplicated, in a stable order."""
    reasons = _page_quality(source, text, tgt)
    if text:
        if meta_speech(text):
            reasons.append("meta_speech")
        if secret_hits(text):
            reasons.append("secret_pattern")
        reasons += injection_reasons(source, text)
    return list(dict.fromkeys(reasons))
