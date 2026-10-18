"""Priority job queue with a single GPU worker (sections 11.2, 12)."""
import itertools
import json
import threading
import time
import uuid
import base64
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config, memory
from . import layout as layouts
from .backends.base import BackendError
from .cache import bbox_key, ocr_key, sha256
from PIL import Image
from .ocr import order_rects
from .presentation import prepare_presentation

FINISHED = {"done", "partial", "failed", "cancelled"}
KEEP_FINISHED_S = 600


def error_code(e: Exception) -> str:
    """What a failed job may show and log: a code or an exception name, never its message. A message can carry
    dialogue (CLI output, model text), and the job error goes to the reader's panel and to metrics.jsonl."""
    code = e.code if isinstance(e, BackendError) else None
    return str(code)[:40] if code else type(e).__name__


class QueueFull(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    session: str
    scope: str
    src: str
    tgt: str
    model: str
    reading: str
    priority: int
    seq: int
    image: bytes | None
    created: float = field(default_factory=time.time)
    status: str = "queued"  # queued -> ocr -> translate -> done | partial | failed | cancelled
    error: str | None = None
    width: int = 0
    height: int = 0
    segments: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    cancelled: bool = False
    finished_at: float | None = None
    image_hash: str = ""
    options: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    layout: dict = field(default_factory=layouts.empty)
    memory: bool = False

    def snapshot(self) -> dict:
        return {
            "id": self.id, "status": self.status, "error": self.error, "image_hash": self.image_hash,
            "src": self.src, "tgt": self.tgt, "model": self.model,
            "width": self.width, "height": self.height,
            "segments": [dict(s) for s in self.segments], "timings": dict(self.timings),
            'quality': dict(self.quality), "layout": self.layout, "memory": self.memory,
        }


# A page makes one translator call, or two on a long page. A call costs about 2.7 s before it says anything, whatever its size, so a
# second call pays only when the reading it overlaps is longer than that: the first PIPELINE_CHUNK balloons go out while the rest is
# read, but only if reading the rest (estimated from the time the first balloons took) still takes PIPELINE_REST_S or more.
# Measured on this PC with a fake 3 s translator (tools/pipeline_bench.py --rest-s 0 forces the split): a page of 24 balloons showed
# its first line 29 % sooner and finished 36 % later; a page of 36 showed it 44 % sooner and finished 18 % later. Reading the rest of
# such a page takes under 2.5 s here, so the helper keeps these pages in one call.
PIPELINE_CHUNK = 8       # balloons read together, and the size of the early translation step
PIPELINE_REST_S = 2.5    # seconds of reading that must still be ahead for the early step to go out
PIPELINE_CONTEXT = 16    # translated lines of the early step that the second one still sees as reference


def _visible_band(job: Job, img):
    """The part of the image the reader is looking at, in pixels, when the extension sent it (options.visible = [top, bottom],
    fractions of the image height). None reads the page in reading order."""
    band = job.options.get("visible")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    try:
        top, bottom = sorted(min(1.0, max(0.0, float(v))) for v in band)
    except (TypeError, ValueError):
        return None
    if bottom <= top:
        return None
    return top * img.height, bottom * img.height


def _merge_meta(total: dict, meta: dict) -> None:
    """Adds one translation step's meta to the page's: counts and times add up, lists join, the first text wins."""
    for k, v in meta.items():
        if k == "usage" and isinstance(v, dict):
            usage = total.setdefault("usage", {})
            for uk, uv in v.items():
                if isinstance(uv, (int, float)):
                    usage[uk] = usage.get(uk, 0) + uv
        elif isinstance(v, bool):
            total[k] = total.get(k, False) or v
        elif isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
        elif isinstance(v, list):
            total.setdefault(k, []).extend(v)
        elif v not in (None, "") and k not in total:
            total[k] = v


class _PageTranslation:
    """The translation steps of one page. A step translates the balloons it is given, with the translated lines of earlier steps
    as fixed reference. Steps run one after another on their own thread, so the next balloons are read while a step is in flight."""

    def __init__(self, manager: "JobManager", job: Job, *, edits: dict, reuse: dict, inputs: dict, vision):
        self._m = manager
        self._job = job
        self._edits = edits
        self._reuse = reuse
        self._inputs = inputs
        self._vision = vision
        self._pool: ThreadPoolExecutor | None = None
        self._futures = []
        self._context = []  # (sid, source, translation) of the translated lines that the next step sees
        self._results = {}  # sid -> (translation, status), as the translator reported them
        self._by_sid = {}  # sid -> the segment dict the page shows
        self._next_sid = 0
        self.translated = {}  # bbox key -> (translation, status), for the final page
        self.meta = {}
        self.started_at = None
        self.steps = 0  # translator calls made for this page

    def submit(self, items):
        """Queues one step for balloons that were just read: (bbox [x, y, w, h], source text, doubts). Empty steps are skipped."""
        if not items:
            return
        step = []
        with self._m._lock:
            for bbox, text, doubts in items:
                sid = self._next_sid
                self._next_sid += 1
                key = bbox_key(bbox)
                text = self._edits["src"].get(key, text)  # the reader's own source text is what gets translated
                seg = {"id": sid, "bbox": list(bbox), "src": text, "conf": 0.0, "status": "pending", "tgt": None}
                if doubts:
                    seg.update(ocr_uncertain=True, ocr_doubts=doubts)
                self._job.segments.append(seg)
                self._by_sid[sid] = seg
                step.append((sid, key, text, bool(doubts)))
            if self.started_at is None:
                self.started_at = time.time()
                self._job.timings["first_step_ms"] = round((self.started_at - self._job.created) * 1000)
                self._job.status = "translate"
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="balloonlens-translate")
        self._futures.append(self._pool.submit(self._step, step))

    def wait(self):
        """Waits for the queued steps. A failed step raises here."""
        futures, self._futures = self._futures, []
        for future in futures:
            future.result()

    def translate_rest(self, segments):
        """Translates what the steps did not cover, once, on the final page with its own ids: a region the reader added in the layout,
        or a line whose step did not finish. Lines already translated go along as fixed lines, so the translator sees the whole page."""
        if all(bbox_key(s["bbox"]) in self.translated or not s["src"].strip() for s in segments):
            return
        with self._m._lock:
            self._by_sid = {s["id"]: s for s in segments}
        lines, fixed, uncertain = [], {}, set()
        for s in segments:
            key = bbox_key(s["bbox"])
            if not s["src"].strip():
                continue
            lines.append((s["id"], s["src"]))
            if s.get("ocr_uncertain"):
                uncertain.add(s["id"])
            if key in self.translated and self.translated[key][1] == "done":
                fixed[s["id"]] = self.translated[key][0]
            elif key in self._edits["tgt"]:
                fixed[s["id"]] = self._edits["tgt"][key]
            elif key in self._reuse and self._reuse[key]["src"] == s["src"]:
                fixed[s["id"]] = self._reuse[key]["tgt"]
        self._results = {}
        self._call(lines, fixed, uncertain)

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def _step(self, step):
        """One translation call: the new balloons, with the recent translated lines of this page as fixed reference."""
        lines, fixed, uncertain = [], {}, set()
        for sid, src, tgt in self._context:
            lines.append((sid, src))
            fixed[sid] = tgt
        for sid, key, text, doubtful in step:
            lines.append((sid, text))
            if doubtful:
                uncertain.add(sid)
            if key in self._edits["tgt"]:
                fixed[sid] = self._edits["tgt"][key]
            elif key in self._reuse and self._reuse[key]["src"] == text:
                fixed[sid] = self._reuse[key]["tgt"]
        self._call(lines, fixed, uncertain)
        context = [(sid, src, self._results[sid][0]) for sid, src in lines
                   if sid in self._results and self._results[sid][1] == "done"]
        self._context = (self._context + context)[-PIPELINE_CONTEXT:]

    def _call(self, lines, fixed, uncertain):
        job = self._job
        # Fixed lines are final already: the page records them here, so it does not depend on what the translator reports.
        for sid, text in fixed.items():
            self._on_result(sid, text, "done")
        translator = self._m.router.translator_for(job.model) if self._m.router else self._m.translator
        meta = translator.translate_page(
            lines, job.src, job.tgt, job.model, job.scope, self._on_result, should_cancel=lambda: job.cancelled,
            context=self._inputs["context"], instructions=self._inputs["instructions"], glossary=self._inputs["glossary"],
            notes=self._inputs["notes"], fixed=fixed, image=self._vision, uncertain=uncertain,
            review=job.options.get("review"),
        )
        self.steps += 1
        _merge_meta(self.meta, meta)

    def _on_result(self, sid, text, status):
        with self._m._lock:
            seg = self._by_sid.get(sid)
            if seg is None or self._job.cancelled:
                return
            seg["tgt"], seg["status"] = text, status
            self._results[sid] = (text, status)
            self.translated[bbox_key(seg["bbox"])] = (text, status)
            self._job.timings.setdefault("first_result_ms", round((time.time() - self._job.created) * 1000))


class JobManager:
    def __init__(self, ocr_engine, translator, cache, decode, metrics_path=None, router=None):
        self.ocr = ocr_engine
        self.translator = translator
        self.router = router  # picks the local or a subscription translator per model (v0.5.0)
        self.cache = cache
        self.decode = decode
        self.metrics_path = metrics_path
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Condition()
        self._seq = itertools.count()
        self._thread = None
        # Subscription jobs run side by side (their CLI calls are the slow part). OCR is one-at-a-time on the GPU.
        self._ocr_lock = threading.Lock()
        self._cli_limit = config.CLI_JOB_SLOTS
        self._cli_active = 0
        self._cli_pool = None

    # ----- API side -----
    def submit(self, image: bytes, *, src, tgt, model, reading, priority, session, scope, options=None) -> Job:
        with self._lock:
            self._prune()
            if sum(1 for j in self._jobs.values() if j.status not in FINISHED) >= config.MAX_QUEUED_JOBS:
                raise QueueFull()
            job = Job(id=uuid.uuid4().hex, session=session, scope=scope, src=src, tgt=tgt, model=model,
                      reading=reading, priority=priority, seq=next(self._seq), image=image, options=options or {})
            self._jobs[job.id] = job
            self._lock.notify()
            return job

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            self._cancel(job)
            return True

    def cancel_session(self, session: str) -> int:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.session == session and j.status not in FINISHED]
            for j in jobs:
                self._cancel(j)
            return len(jobs)

    def set_priority(self, job_id: str, priority: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.priority = priority
            return True

    def queue_depth(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status not in FINISHED)

    def _cancel(self, job: Job):
        job.cancelled = True
        if job.status == "queued":
            self._finish(job, "cancelled")

    def _finish(self, job: Job, status: str, error: str | None = None):
        job.status, job.error, job.finished_at, job.image = status, error, time.time(), None

    def _prune(self):
        now = time.time()
        for jid in [j.id for j in self._jobs.values() if j.finished_at and now - j.finished_at > KEEP_FINISHED_S]:
            del self._jobs[jid]

    # ----- worker side -----
    def start(self):
        self._cli_pool = ThreadPoolExecutor(max_workers=self._cli_limit, thread_name_prefix="balloonlens-cli")
        self._thread = threading.Thread(target=self._loop, name="balloonlens-worker", daemon=True)
        self._thread.start()

    def _is_cli(self, job: Job) -> bool:
        return self.router is not None and self.router.is_subscription(job.model)

    def _next(self) -> Job:
        with self._lock:
            while True:
                # A subscription job waits in the queue while every CLI slot is busy; local jobs never wait for it.
                queued = [j for j in self._jobs.values()
                          if j.status == "queued" and (not self._is_cli(j) or self._cli_active < self._cli_limit)]
                if queued:
                    job = min(queued, key=lambda j: (j.priority, j.seq))
                    job.status = "ocr"
                    if self._is_cli(job):
                        self._cli_active += 1
                    return job
                self._lock.wait()

    def _loop(self):
        while True:
            job = self._next()
            if self._is_cli(job):
                self._cli_pool.submit(self._run_cli_job, job)
            else:
                self.process(job)

    def _run_cli_job(self, job: Job):
        try:
            self.process(job)
        finally:
            with self._lock:
                self._cli_active -= 1
                self._lock.notify_all()

    def process(self, job: Job):
        t_start = time.time()
        job.timings["queue_ms"] = round((t_start - job.created) * 1000)
        try:
            self._run(job, t_start)
        except Exception as e:  # keep the worker alive; surface the reason
            with self._lock:
                self._finish(job, "failed", error_code(e))
        job.timings["total_ms"] = round((time.time() - job.created) * 1000)
        self._write_metrics(job)

    def _run(self, job: Job, t_start: float):
        # A subscription call goes out only when usage limits and the CLI's own state allow it.
        t_gate = time.perf_counter()
        blocked = self.router.gate(job.model) if self.router and self.router.is_subscription(job.model) else None
        job.timings["gate_ms"] = round((time.perf_counter() - t_gate) * 1000)
        if blocked:
            job.timings["blocked_by"] = blocked
            with self._lock:
                self._finish(job, "failed", blocked)
            return
        image_hash = sha256(job.image)
        t_decode = time.perf_counter()
        img = self.decode(job.image)
        job.timings["decode_ms"] = round((time.perf_counter() - t_decode) * 1000)
        job.width, job.height, job.image_hash = img.width, img.height, image_hash
        ocr_version = config.OCR_VERSION
        if job.src == "auto":
            ocr_version += ":" + self.ocr.auto.signature
        key = ocr_key(image_hash, job.src, f"{ocr_version}:{job.reading}")

        def read_region(bbox):
            region_key = ocr_key(image_hash, job.src, f"{ocr_version}:region:{bbox_key(bbox)}")
            hit = self.cache.get_ocr(region_key)
            if hit is None:
                text, conf, uncertain = self.ocr.read_region(img, bbox, job.src)
                hit = {"text": text, "conf": conf, "uncertain": uncertain}
                self.cache.put_ocr(region_key, hit)
            return hit["text"], hit["conf"], hit["uncertain"]

        saved_layout = self.cache.get_layout(image_hash, job.src)
        job.layout = saved_layout or layouts.empty()
        # The edits, the memory, and the vision thumbnail do not depend on the OCR text, so they are ready before the first read.
        t_memory = time.perf_counter()
        edits = self.cache.get_edits(image_hash, job.src, job.tgt)
        inputs = memory.translation_inputs(self.cache, job.scope, job.src, job.tgt, job.model, job.options, image_hash)
        job.memory = inputs["memory"]
        # Same image, same rules: stored translations are reused instead of running the model again.
        reuse = memory.reuse_lines(self.cache, job.scope, image_hash, job.src, job.tgt, inputs["rules"]) if job.memory else {}
        job.timings["memory_ms"] = round((time.perf_counter() - t_memory) * 1000)
        # CLI startup overlaps reading the image, including the first page after an idle period.
        translator = self.router.translator_for(job.model) if self.router else self.translator
        prepare = getattr(translator, "prepare", None)
        if prepare is not None and not job.cancelled:
            prepare(job.src, job.tgt, job.model, instructions=inputs["instructions"])
        vision_image = None
        if job.options.get('vision') and job.model.startswith('qwen3.5'):
            thumb = img.copy()  # the thumbnail must not change the page that OCR still reads
            thumb.thumbnail((1280, 1920))
            buf = io.BytesIO()
            thumb.save(buf, 'JPEG', quality=85)
            vision_image = base64.b64encode(buf.getvalue()).decode('ascii')
        job.image = None

        pipe = _PageTranslation(self, job, edits=edits, reuse=reuse, inputs=inputs, vision=vision_image)
        try:
            raw = self._read_page(job, img, key, pipe)
            # The whole page is read. Translation steps started earlier may still be running.
            job.timings["ocr_done_ms"] = round((time.time() - job.created) * 1000)
            if job.cancelled:
                pipe.wait()
                with self._lock:
                    self._finish(job, "cancelled")
                return
            # Layout and cleanup depend on the OCR boxes, not Claude's answer. Prepare them while it responds.
            t_layout = time.perf_counter()
            segments = layouts.apply_layout(raw, saved_layout, read_region, job.reading)
            t_present = time.perf_counter()
            segments = layouts.apply_frames(prepare_presentation(img, segments))
            t_wait = time.perf_counter()
            job.timings.update(layout_ms=round((t_present - t_layout) * 1000),
                               present_ms=round((t_wait - t_present) * 1000))
            pipe.wait()
            job.timings["translate_wait_ms"] = round((time.perf_counter() - t_wait) * 1000)
            t_rest = time.perf_counter()
            pipe.translate_rest(segments)
            job.timings["translate_rest_ms"] = round((time.perf_counter() - t_rest) * 1000)
        finally:
            pipe.close()
        del img
        job.timings["steps"] = pipe.steps
        if pipe.started_at is not None:
            job.timings["translate_ms"] = round((time.time() - pipe.started_at) * 1000)

        merged = []
        for s in segments:
            key_bbox = bbox_key(s["bbox"])
            seg = dict(s, ocr_src=s["src"], tgt=None, status="pending", edited=False)
            if key_bbox in edits["src"]:
                seg["src"] = edits["src"][key_bbox]
            if key_bbox in edits["tgt"]:
                seg.update(tgt=edits["tgt"][key_bbox], status="done", edited=True)
            elif key_bbox in reuse and reuse[key_bbox]["src"] == seg["src"]:
                seg.update(tgt=reuse[key_bbox]["tgt"], reused=True)
            elif key_bbox in pipe.translated:
                seg["tgt"], seg["status"] = pipe.translated[key_bbox]
            if not seg["src"].strip() and not seg["edited"]:
                seg["status"] = "review"  # nothing readable in a reader-drawn box: ask, don't guess
            merged.append(seg)

        meta = pipe.meta
        job.quality = {k: meta[k] for k in ('quality_errors', 'quality_note') if k in meta}
        job.timings["model_load_ms"] = meta.get("load_ms", 0)
        job.timings["tr_cache_hits"] = meta.get("cache_hits", 0)
        # Claude call time split: cli_ms = wall time of the calls, api_ms = the API part the CLI reports,
        # slot_wait_ms / gap_wait_ms = waits before a call starts, warm_calls = calls answered by a process started
        # ahead of time (all zero for local models).
        for name in ("cli_ms", "api_ms", "slot_wait_ms", "gap_wait_ms", "warm_calls"):
            job.timings[name] = meta.get(name, 0)
        job.timings["reused_lines"] = sum(1 for s in merged if s.get("reused"))
        job.timings["context_lines"] = len(inputs["context"])
        usage = meta.get("usage") or {}
        if usage:
            job.timings.update(tokens_in=usage.get("input", 0), tokens_out=usage.get("output", 0),
                               tokens_cached=usage.get("cached", 0))
        for k in ("proofread_calls", "proofread_lines", "proofread_skipped"):
            if k in meta:
                job.timings[k] = meta[k]
        uncertain = {s["id"] for s in merged if s.get("ocr_uncertain")}

        with self._lock:
            job.segments = merged
            # A doubtful reading stays visible but is marked for the reader to check.
            for s in job.segments:
                if s["id"] in uncertain and not s["edited"] and s["status"] == "done":
                    s["status"] = "review"
                    job.quality.setdefault("quality_errors", []).append({"id": s["id"], "reasons": ["ocr_uncertain"]})
            if job.cancelled:
                self._finish(job, "cancelled")
                return
            if any(s["status"] in ("failed", "pending") for s in job.segments):
                self._finish(job, "partial", meta.get("error"))
            else:
                self._finish(job, "done")
            segments_done = [dict(s) for s in job.segments]
        if job.memory:
            memory.remember_page(self.cache, job.scope, image_hash, job.src, job.tgt,
                                 job.options.get("work") or {}, inputs["rules"], segments_done)

    def _read_page(self, job: Job, img: Image.Image, key: str, pipe: "_PageTranslation"):
        """Returns the page's segments in reading order with their final ids. A page that is already cached goes to the translator at once,
        in one call, without waiting for the reading lock. Otherwise the balloons are read under the lock, and a long page's first balloons
        go to the translator while the rest is read. A job that waited for the lock checks the cache again: another job may have read it."""
        cached = self.cache.get_ocr(key)
        if cached is None:
            visible = _visible_band(job, img)
            t_lock = time.perf_counter()
            with self._ocr_lock:
                job.timings["ocr_lock_wait_ms"] = round((time.perf_counter() - t_lock) * 1000)
                cached = self.cache.get_ocr(key)
                if cached is None:
                    raw = self._read_balloons(job, img, pipe, visible)
                    if not job.cancelled:
                        self.cache.put_ocr(key, raw)
                    return raw
        job.timings["ocr_cache"] = True
        if not job.cancelled:  # nothing is being read, so there is nothing to overlap: one call for the page
            pipe.submit([(s["bbox"], s["src"], s.get("ocr_doubts")) for s in cached])
        return cached

    def _read_balloons(self, job: Job, img: Image.Image, pipe: "_PageTranslation", visible) -> list:
        """Reads every balloon of the page and returns the segments in reading order. The caller holds the OCR lock. The balloons are
        read as the OCR engine finds them (the ones at `visible` first), PIPELINE_CHUNK at a time. They go to the translator in one
        call when the page is read; on a long page the first PIPELINE_CHUNK go out earlier, while the rest is read. A cancelled job
        keeps what it read, so the next job for the same page starts from there."""
        partial_version = config.OCR_VERSION + (":" + self.ocr.auto.signature if job.src == "auto" else "")
        partial_key = ocr_key(job.image_hash, job.src, f"{partial_version}:partial")
        known = {tuple(item["box"]): (item["text"], item["conf"], item["doubts"], 0)
                 for item in self.cache.get_ocr(partial_key) or []}
        read, queue, stats, marks = {}, [], {}, {}
        pending, early, fresh_count = [], False, 0  # pending: read balloons the translator has not got yet ([x, y, w, h], text, doubts)
        recognize_ms = 0
        t_block = time.perf_counter()

        def read_chunk(chunk):
            nonlocal recognize_ms, early, fresh_count
            fresh = [span for span in chunk if tuple(span[1]) not in known]
            t_read = time.perf_counter()
            done = {}
            if fresh:
                done = {tuple(span[1]): item[1:] for span, item in zip(fresh, self.ocr.read_spans(img, fresh, job.src, stats))}
            recognize_ms += round((time.perf_counter() - t_read) * 1000)
            fresh_count += len(fresh)
            items = []
            for _, box in chunk:
                k = tuple(box)
                read[k] = done[k] if k in done else known[k]
                text, _conf, doubts, _retries = read[k]
                if visible and box[1] < visible[1] and box[3] > visible[0]:
                    marks["visible"] = time.time()
                if text:
                    items.append(([box[0], box[1], box[2] - box[0], box[3] - box[1]], text, doubts))
            job.timings.setdefault("first_read_ms", round((time.time() - job.created) * 1000))
            pending.extend(items)
            # The early step goes out only when reading the balloons still to be read takes longer than a call's fixed cost, at the
            # speed the ones read so far went; a shorter page waits and goes in one call.
            if (not early and len(pending) >= PIPELINE_CHUNK and fresh_count
                    and len(queue) * recognize_ms / fresh_count / 1000 >= PIPELINE_REST_S):
                early = True
                pipe.submit(pending[:PIPELINE_CHUNK])
                del pending[:PIPELINE_CHUNK]

        final = None
        for step in self.ocr.plan_steps(img, job.src, job.reading, visible=visible):
            queue += step.spans
            # A chunk is read when it is full, or when the page is planned and the last balloons are left.
            while queue and (len(queue) >= PIPELINE_CHUNK or step.final) and not job.cancelled:
                chunk, queue = queue[:PIPELINE_CHUNK], queue[PIPELINE_CHUNK:]
                read_chunk(chunk)
            if step.final:
                final = step
            if step.final or job.cancelled:
                break
        if not job.cancelled:
            pipe.submit(pending)  # the whole page, or what the early step left
        timings = job.timings
        timings["recognize_ms"] = recognize_ms
        if "auto_scripts" in stats:
            timings["ocr_scripts"] = stats["auto_scripts"]
        timings["ocr_retries"] = sum(item[3] for item in read.values())
        timings.update(ocr_retry_spans=stats.get("retry_spans", 0), ocr_retry_fixed=stats.get("retry_fixed", 0),
                       ocr_retry_ms=stats.get("retry_ms", 0), ocr_resumed=sum(1 for k in read if k in known))
        timings["ocr_block_ms"] = round((time.perf_counter() - t_block) * 1000)
        if "visible" in marks:
            timings["visible_read_ms"] = round((marks["visible"] - job.created) * 1000)
        if job.cancelled or final is None:
            kept = {**known, **read}
            if len(kept) > len(known):
                self.cache.put_ocr(partial_key, [{"box": list(k), "text": v[0], "conf": v[1], "doubts": v[2]}
                                                 for k, v in kept.items()])
            return []
        if known:
            self.cache.put_ocr(partial_key, [])  # the page is whole now; what an earlier job kept is no longer needed
        timings["detect_ms"] = final.stats["detect_ms"]
        timings["boxes"], timings["groups"], timings["tiles"] = final.stats["boxes"], final.stats["groups"], final.stats["tiles"]
        # The final ids follow the page's reading order, exactly as a single read gives them.
        candidates = [(box, *read[tuple(box)][:3]) for _, box in final.all_spans if read[tuple(box)][0]]
        final_order = order_rects([c[0] for c in candidates], job.reading)
        raw = []
        for new_id, idx in enumerate(final_order):
            box, text, conf, doubts = candidates[idx]
            seg = {"id": new_id, "bbox": [box[0], box[1], box[2] - box[0], box[3] - box[1]], "src": text, "conf": conf}
            if doubts:
                seg.update(ocr_uncertain=True, ocr_doubts=doubts)
            raw.append(seg)
        return raw

    def _write_metrics(self, job: Job):
        if not self.metrics_path:
            return
        # No image or dialogue text in diagnostics (section 14.3).
        row = {"ts": round(time.time()), "status": job.status, "error": job.error, "src": job.src, "tgt": job.tgt,
               "model": job.model, "w": job.width, "h": job.height, "segments": len(job.segments),
               "chars": sum(len(s["src"]) for s in job.segments), **job.timings}
        try:
            with open(self.metrics_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass
