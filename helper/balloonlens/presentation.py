"""Non-destructive text cleanup and fitting inside verified light speech bubbles.

Pixel-based enclosed-region detection is deliberately conservative. Open bubbles,
artwork and dark backgrounds keep the overlay fallback instead of being erased.
"""
import base64
import io

import cv2
import numpy as np
from PIL import Image

VERSION = 'enclosed-bubble-v1'


def _inner_rect(mask):
    """Largest axis-aligned rectangle inside the mask, at bounded analysis size."""
    h, w = mask.shape
    scale = min(1.0, 240 / max(w, h))
    small = cv2.resize(mask, (max(1, round(w*scale)), max(1, round(h*scale))), interpolation=cv2.INTER_AREA)
    small = small > 254
    heights = np.zeros(small.shape[1], dtype=int)
    best_area, best = 0, None
    for y, row in enumerate(small):
        heights = np.where(row, heights + 1, 0)
        stack = []
        for x in range(len(heights) + 1):
            value = int(heights[x]) if x < len(heights) else 0
            start = x
            while stack and stack[-1][1] > value:
                left, height = stack.pop()
                # Korean reads horizontally: a wider, shorter frame is preferable
                # to the largest tall rectangle inherited from Japanese columns.
                fit_height = min(height, max(1, round((x-left)/0.72)))
                area = (x-left)*fit_height
                if area > best_area:
                    best_area, best = area, (left, y-height+1+(height-fit_height)//2, x-left, fit_height)
                start = left
            if value and (not stack or stack[-1][1] < value):
                stack.append((start, value))
    if best is None:
        return None
    x, y, rw, rh = [round(n/scale) for n in best]
    pad = max(3, round(min(rw, rh)*0.04))
    return [x+pad, y+pad, max(1, rw-2*pad), max(1, rh-2*pad)]


def prepare_presentation(image, segments):
    if not segments:
        return []
    rgb = np.asarray(image.convert('RGB'))
    H, W = rgb.shape[:2]
    ratio = min(1.0, 2200/max(H, W))
    analysis = cv2.resize(rgb, (max(1, round(W*ratio)), max(1, round(H*ratio))), interpolation=cv2.INTER_AREA)
    # White/near-white interiors, not grey artwork or saturated color regions.
    light = ((analysis.min(axis=2) >= 225) & (np.ptp(analysis, axis=2) < 25)).astype('uint8')
    _, labels, stats, _ = cv2.connectedComponentsWithStats(light, connectivity=8)
    cache, out, shared = {}, [], {}
    for segment in segments:
        s = dict(segment)
        s['presentation'] = {'kind': 'overlay', 'reason': 'unverified_bubble'}
        x, y, w, h = s['bbox']
        ax, ay, aw, ah = [round(v*ratio) for v in (x, y, w, h)]
        pad = max(4, round(min(aw, ah)*0.3))
        l, t = max(0, ax-pad), max(0, ay-pad)
        r, b = min(labels.shape[1], ax+aw+pad), min(labels.shape[0], ay+ah+pad)
        ids, counts = np.unique(labels[t:b, l:r], return_counts=True)
        candidates = sorted(zip(ids, counts), key=lambda pair: -pair[1])
        chosen = None
        for cid, _ in candidates:
            if cid == 0:
                continue
            bx, by, bw, bh, area = stats[cid]
            if bx <= 0 or by <= 0 or bx+bw >= labels.shape[1] or by+bh >= labels.shape[0]:
                continue
            if area < max(80, aw*ah*0.7) or area > light.size*0.55:
                continue
            if not (bx <= ax+aw/2 < bx+bw and by <= ay+ah/2 < by+bh):
                continue
            if cid not in cache:
                component = (labels[by:by+bh, bx:bx+bw] == cid).astype('uint8')*255
                contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                filled = np.zeros_like(component)
                cv2.drawContours(filled, contours, -1, 255, cv2.FILLED)
                filled = cv2.erode(filled, np.ones((5, 5), np.uint8))
                frame = _inner_rect(filled)
                color = np.median(analysis[by:by+bh, bx:bx+bw][component > 0], axis=0).astype('uint8')
                cache[cid] = (filled, frame, color)
            filled, frame, color = cache[cid]
            ix1, iy1 = max(0, ax-bx), max(0, ay-by)
            ix2, iy2 = min(bw, ax+aw-bx), min(bh, ay+ah-by)
            region = filled[iy1:iy2, ix1:ix2]
            if not frame or region.size < aw*ah*0.8 or np.count_nonzero(region) < max(1, aw*ah)*0.8:
                continue
            chosen = cid
            break
        if chosen is not None:
            shared.setdefault(int(chosen), []).append(s)
            bx, by, bw, bh, _ = stats[chosen]
            filled, frame, color = cache[chosen]
            # Cleanup only around the OCR glyphs, never the entire bubble or source image.
            p = max(3, round(min(w, h)*0.14))
            cx, cy = max(0, x-p), max(0, y-p)
            ex, ey = min(W, x+w+p), min(H, y+h+p)
            yy, xx = np.mgrid[cy:ey, cx:ex]
            mx = np.clip(np.round(xx*ratio).astype(int)-bx, 0, bw-1)
            my = np.clip(np.round(yy*ratio).astype(int)-by, 0, bh-1)
            interior = filled[my, mx] > 0
            interior &= (xx*ratio >= bx) & (xx*ratio < bx+bw) & (yy*ratio >= by) & (yy*ratio < by+bh)
            for other in segments:
                if other['id'] == s['id']:
                    continue
                ox, oy, ow, oh = other['bbox']
                interior &= ~((xx >= ox) & (xx < ox+ow) & (yy >= oy) & (yy < oy+oh))
            crop = rgb[cy:ey, cx:ex]
            ink = (np.max(np.abs(crop.astype(int)-color.astype(int)), axis=2) > 1).astype('uint8')
            ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))
            patch = np.zeros((ey-cy, ex-cx, 4), dtype='uint8')
            patch[:, :, :3] = color
            patch[:, :, 3] = ((ink > 0) & interior).astype('uint8')*255
            buf = io.BytesIO()
            Image.fromarray(patch).save(buf, 'PNG')
            fx, fy, fw, fh = frame
            s['presentation'] = {
                'kind': 'bubble', 'method': VERSION,
                'frame': [round((bx+fx)/ratio), round((by+fy)/ratio), round(fw/ratio), round(fh/ratio)],
                'cleanup_bbox': [cx, cy, ex-cx, ey-cy],
                'cleanup': 'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode('ascii'),
            }
        out.append(s)
    # Several OCR groups may belong to one bubble. Give them disjoint rows,
    # retaining separate IDs and source boxes so corrections remain compatible.
    for group in shared.values():
        if len(group) < 2:
            continue
        x, y, w, h = group[0]['presentation']['frame']
        for i, s in enumerate(group):
            top = y + round(h*i/len(group))
            bottom = y + round(h*(i+1)/len(group))
            s['presentation']['frame'] = [x, top+2, w, max(1, bottom-top-4)]
    return out
