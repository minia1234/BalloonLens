# Convenience and follow-ups (0.4.0)

> Not a Git checkout. Scope chosen from 0.3.0's known gaps; confirm with the reader before widening it.

- [x] Moving/resizing a text region carries the reader's source/translation fixes to the new box (`moved_from`, consumed on save).
- [x] Selective vs full review comparison tool with warm-up and alternating order (`tools/review_compare.py`).
- [x] Editor: hide labels of boxes not being edited.
- [x] Dialogue list panel: reading order by image, search, filters (all / needs checking / edited), jump to balloon, copy visible lines; pill button, popup button, `Alt+Shift+L`.
- [x] Popup shortcut to the region editor.
- [x] Settings backup/restore with value validation (`validateBackup`, unit-tested).
- [x] Evidence: Python 83, Node 27, regions/convenience/regression E2E, comparison report, README, validation report, version 0.4.0, package.

Not done / not claimed: real-page quality evaluation; split/merge regions don't inherit fixes.
