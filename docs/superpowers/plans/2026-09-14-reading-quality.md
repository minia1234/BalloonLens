# Reading quality implementation plan

> Execute inline with executing-plans and test-driven-development. This directory is not a Git checkout; preserve existing work and do not invent commits.

**Goal:** Context-aware Korean dialogue and readable, cleaned speech bubbles in the existing Chrome extension.

**Architecture:** Keep the local helper authoritative. Add a page-level translator with bounded context, per-work instructions, contextual cache keys and explicit review status. Analyze enclosed light speech bubbles from source pixels and return separate cleanup patches and safe text frames; keep original pixels recoverable. Use text-only local Qwen by default for quality; retain Gemma as a fast option. Expose optional image-capable model support only when supported by the local runtime. No cloud calls.

**Tech stack:** Python/FastAPI/Ollama, OpenCV/Pillow, MV3 JavaScript and browser DOM text measurement.

## 1. Translation
- [x] Add failing tests for cached/missing/user-edited lines remaining in reference context, work instructions, numeric omissions, bounded retries, cancellation, and changed context invalidating cache.
- [x] Add `translate_page()` in `helper/balloonlens/translate.py` while preserving `translate()` for single-line compatibility. A full page is reference context, batches contain stable target IDs; caller supplies fixed edited translations. Use all context in cache hashing. Output missing/duplicate IDs are rejected; numeric/language problems get one targeted retry and visible review state.
- [x] Add validated instructions and glossary strings to job and line API, plus prior-page context supplied by the content script (only complete earlier DOM images). Preserve user-edited targets and semantic original OCR.
- [x] Pass Python tests using `helper/.venv/Scripts/python.exe -m pytest -q`. (64 passed)

## 2. Bubble presentation
- [x] Add `helper/balloonlens/presentation.py` and tests: closed white oval, no bubble, dark/colored background, adjacent bubbles, source preservation, frame containment.
- [x] Find enclosed light connected components, fill glyph holes, reject page background and ambiguous regions. Derive a text frame inside an eroded contour. Remove glyph pixels only inside the verified bubble with an alpha cleanup patch. Mark unsupported backgrounds honestly and retain existing overlay fallback.
- [x] Integrate into jobs separately from OCR cache so cached OCR still produces current presentation. Preserve raw bboxes for edits.
- [x] Render cleanup as a separate layer; fit Korean at word boundaries with balanced lines inside the safe frame. Hide cleanup when translation is absent or original peek is on. Refit on resize and font settings without rerunning inference.
  - Wrap-up fix: framed text no longer loses an extra 5px per side, and words shrink to an 11px floor before being split. The E2E now fails on one-syllable lines.

## 3. Product integration and evidence
- [x] Add per-work instructions/glossary controls to options; choose work by URL path with user override. Pass page context on automatic and single-line retranslations.
- [x] Add meaningful Node/browser regressions for fitting, original restoration and stale jobs. Run isolated real helper and existing extension E2E. (Node 11 passed; final E2E `bench_out/e2e/1789388718945087300`)
- [x] Compare same samples with baseline, inspect final images and document exactly what was measured. Only 2 synthetic samples were compared. Real comic quality evaluation is still open (see QUALITY_VALIDATION.md).
- [x] Update README, validation report, extension version and user-installable ZIP. (`release/BalloonLens-0.2.0.zip`, 63 files, checked for corruption and venv/cache leaks)

## Acceptance
No lost source/corrected text; no cache result reused after context/rules change; uncertain output is review, not silently accepted; verified white bubble interiors do not show original glyphs or erase outlines; translated text stays in its authored frame at ordinary reading size; original peek restores all source pixels; stop/reload/edit flows continue to work.

Status: met on synthetic pages only. Not yet shown on real comics: whether translations are accurate and whether 11px text in tiny bubbles is comfortable to read.
