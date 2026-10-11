"""Reader corrections to detected regions.

A layout for one image has three independent parts:
- removed: OCR regions (by bbox key) the reader deleted, moved, split or merged away
- added:   reader-drawn text regions, each OCR'd on its own
- frames:  translation placement boxes for surviving OCR regions (added regions carry their own)
Where the source glyphs are and where the translation goes are deliberately separate.
"""
from .cache import bbox_key
from .ocr import order_rects


def empty() -> dict:
    return {"removed": [], "added": [], "frames": {}}


def is_empty(layout: dict | None) -> bool:
    return not layout or not (layout.get("removed") or layout.get("added") or layout.get("frames"))


def apply_layout(segments, layout, read_region, reading):
    """segments: cached OCR output. read_region(bbox) -> (text, conf, uncertain).

    Returns segments renumbered in reading order, each with an `origin` the editor can refer back to.
    """
    layout = layout or empty()
    removed = set(layout.get("removed", []))
    frames = layout.get("frames", {})
    kept = []
    for s in segments:
        key = bbox_key(s["bbox"])
        if key in removed:
            continue
        seg = dict(s, origin={"kind": "ocr", "key": key})
        if key in frames:
            seg["user_frame"] = list(frames[key])
        kept.append(seg)
    for region in layout.get("added", []):
        text, conf, uncertain = read_region(region["bbox"])
        seg = {"bbox": list(region["bbox"]), "src": text or "", "conf": conf,
               "origin": {"kind": "user", "uid": region["uid"]}}
        if uncertain:
            seg["ocr_uncertain"] = True
        if region.get("frame"):
            seg["user_frame"] = list(region["frame"])
        kept.append(seg)
    order = order_rects([[x, y, x + w, y + h] for x, y, w, h in (s["bbox"] for s in kept)], reading)
    return [dict(kept[i], id=new_id) for new_id, i in enumerate(order)]


def apply_frames(segments):
    """A reader-set translation box replaces the automatic one and is used in full."""
    out = []
    for s in segments:
        frame = s.pop("user_frame", None)
        if frame:
            s["presentation"] = {**(s.get("presentation") or {}), "frame": frame, "fill": True}
        out.append(s)
    return out
