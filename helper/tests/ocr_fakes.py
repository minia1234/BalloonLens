"""Test doubles for the OCR engine's step interface (plan_steps and read_spans), built on a fake that reads a whole page in run().

The job reads a page in steps: plan_steps() finds the balloons, read_spans() reads a few of them. A fake that returns the whole page
from run() gets the same behaviour here: each balloon is one span, in the order run() returned them, and the page is planned in one
step."""
from balloonlens.ocr import PlanStep


class PageOcr:
    def plan(self, img, src_lang, reading, visible=None):
        segments, _ = self.run(img, src_lang, reading)
        spans = []
        for seg in segments:
            x, y, w, h = seg["bbox"]
            spans.append(([seg], [x, y, x + w, y + h]))
        return spans, list(range(len(spans))), {"boxes": len(spans), "groups": len(spans), "tiles": 1, "detect_ms": 0}

    def plan_steps(self, img, src_lang, reading, visible=None):
        spans, order, stats = self.plan(img, src_lang, reading, visible)
        yield PlanStep([spans[i] for i in order], True, spans, order, stats)

    def read_spans(self, img, spans, src_lang, stats=None):
        out = []
        for members, box in spans:
            seg = members[0]
            doubts = seg.get("ocr_doubts") or (["uncertain"] if seg.get("ocr_uncertain") else [])
            out.append((box, seg["src"], seg.get("conf"), doubts, 0))
        return out
