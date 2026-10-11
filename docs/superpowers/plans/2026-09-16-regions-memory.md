# Region editing, work memory, OCR retry, selective review

> Reference: ComicTL (region editor, recent-page memory, OCR re-read). Ideas only; no code copied. Not a Git checkout.

**Goal:** Let the reader fix wrong regions directly, keep names and tone consistent across pages of a work, re-read only doubtful OCR, and review only lines that need it.

## 1. Region editing
- [x] Helper stores a per-image layout (`removed` OCR boxes, `added` reader regions, translation `frames`), separate from text edits. (`layout.py`, `/v1/layouts`)
- [x] Jobs apply the layout after cached OCR; added regions are OCR'd alone (cached per box); ids follow reading order; reader frames override the placement box.
- [x] Extension editor: select, move, resize, add, split, merge, delete, undo/redo; separate text-region and translation-box modes; save and re-translate the image. (`editor.js`, pure ops in `tools/test_editor.cjs`)

## 2. Work memory
- [x] Only pages matching a registered work URL use memory.
- [x] Store reader-confirmed title / synopsis / characters and each page's source + translation + edited flag. (`memory.py`, `/v1/works`)
- [x] Context = nearest earlier pages within a character budget; numbered addresses never use later pages; edited lines are marked confirmed.
- [x] Revisiting an image with unchanged rules reuses its stored translations.
- [x] Suggestions are shown as unconfirmed until the reader saves them. Popover can add a glossary term.

## 3. OCR partial retry
- [x] Flag empty / letterless / repeated / low-confidence / too-short readings; retry only that crop; keep the best; mark the rest for review.

## 4. Selective review
- [x] Qwen3.5 proofreads only lines with triggers; "review every line" stays available.
- [x] Prefetch deferred images in the service worker.

## Evidence
- [x] Python 82 and Node 25 tests; `tools/e2e_regions.py` (real extension, isolated real helper, Qwen3.5); existing regression and convenience E2E; README, validation report, version 0.3.0, package.
- Not claimed: quality/speed effect of selective review, memory benefit on real serials, OCR retry recovery on real failures. Needs real pages.
