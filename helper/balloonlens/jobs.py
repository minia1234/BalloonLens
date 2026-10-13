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
        t_block = time.perf_counter()
        t_decode = time.perf_counter()
        img = self.decode(job.image)
        job.timings["decode_ms"] = round((time.perf_counter() - t_decode) * 1000)
        job.width, job.height, job.image_hash = img.width, img.height, image_hash
        key = ocr_key(image_hash, job.src, f"{config.OCR_VERSION}:{job.reading}")
        segments = self.cache.get_ocr(key)
        if segments is None:
            # Reading the image is the GPU step: only one job reads at a time. A page read before skips this wait.
            t_lock = time.perf_counter()
            with self._ocr_lock:
                job.timings["ocr_lock_wait_ms"] = round((time.perf_counter() - t_lock) * 1000)
                segments, t = self.ocr.run(img, job.src, job.reading)
            self.cache.put_ocr(key, segments)
            job.timings.update(t)
        else:
            job.timings["ocr_cache"] = True

        def read_region(bbox):
            region_key = ocr_key(image_hash, job.src, f"{config.OCR_VERSION}:region:{bbox_key(bbox)}")
            hit = self.cache.get_ocr(region_key)
            if hit is None:
                text, conf, uncertain = self.ocr.read_region(img, bbox, job.src)
                hit = {"text": text, "conf": conf, "uncertain": uncertain}
                self.cache.put_ocr(region_key, hit)
            return hit["text"], hit["conf"], hit["uncertain"]

        saved_layout = self.cache.get_layout(image_hash, job.src)
        segments = layouts.apply_layout(segments, saved_layout, read_region, job.reading)
        job.layout = saved_layout or layouts.empty()
        t_present = time.time()
        segments = layouts.apply_frames(prepare_presentation(img, segments))
        job.timings['presentation_ms'] = round((time.time()-t_present)*1000)
        vision_image = None
        if job.options.get('vision') and job.model.startswith('qwen3.5'):
            img.thumbnail((1280, 1920))
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=85)
            vision_image = base64.b64encode(buf.getvalue()).decode('ascii')
        del img
        job.timings["ocr_block_ms"] = round((time.perf_counter() - t_block) * 1000)

        # The reader's own fixes win over OCR and are never overwritten by auto translation (section 3.4).
        t_memory = time.perf_counter()
        edits = self.cache.get_edits(image_hash, job.src, job.tgt)
        inputs = memory.translation_inputs(self.cache, job.scope, job.src, job.tgt, job.model, job.options, image_hash)
        job.memory = inputs["memory"]
        # Same image, same rules: stored translations are reused instead of running the model again.
        reuse = memory.reuse_lines(self.cache, job.scope, image_hash, job.src, job.tgt, inputs["rules"]) if job.memory else {}
        job.timings["memory_ms"] = round((time.perf_counter() - t_memory) * 1000)
        merged = []
        for s in segments:
            key = bbox_key(s["bbox"])
            seg = dict(s, ocr_src=s["src"], tgt=None, status="pending", edited=False)
            if key in edits["src"]:
                seg["src"] = edits["src"][key]
            if key in edits["tgt"]:
                seg.update(tgt=edits["tgt"][key], status="done", edited=True)
            elif key in reuse and reuse[key]["src"] == seg["src"]:
                seg.update(tgt=reuse[key]["tgt"], reused=True)
            if not seg["src"].strip() and not seg["edited"]:
                seg["status"] = "review"  # nothing readable in a reader-drawn box: ask, don't guess
            merged.append(seg)

        with self._lock:
            job.image = None
            job.segments = merged
            if job.cancelled:
                self._finish(job, "cancelled")
                return
            job.status = "translate"
        t_tr = time.time()
        job.timings["ocr_done_ms"] = round((t_tr - job.created) * 1000)

        by_id = {s["id"]: s for s in job.segments}

        def on_result(sid, text, status):
            with self._lock:
                if job.cancelled:
                    return
                by_id[sid]["tgt"], by_id[sid]["status"] = text, status
                job.timings.setdefault("first_result_ms", round((time.time() - job.created) * 1000))

        uncertain = {s["id"] for s in merged if s.get("ocr_uncertain")}
        translator = self.router.translator_for(job.model) if self.router else self.translator
        meta = translator.translate_page(
            [(s["id"], s["src"]) for s in merged if s["src"].strip()], job.src, job.tgt, job.model, job.scope,
            on_result, should_cancel=lambda: job.cancelled,
            context=inputs["context"], instructions=inputs["instructions"], glossary=inputs["glossary"],
            notes=inputs["notes"], fixed={s['id']: s['tgt'] for s in merged if s['edited'] or s.get('reused')},
            image=vision_image, uncertain=uncertain, review=job.options.get("review"),
        )
        job.quality = {k: meta[k] for k in ('quality_errors', 'quality_note') if k in meta}
        job.timings["translate_ms"] = round((time.time() - t_tr) * 1000)
        job.timings["model_load_ms"] = meta.get("load_ms", 0)
        job.timings["tr_cache_hits"] = meta.get("cache_hits", 0)
        job.timings["reused_lines"] = sum(1 for s in merged if s.get("reused"))
        job.timings["context_lines"] = len(inputs["context"])
        usage = meta.get("usage") or {}
        if usage:
            job.timings.update(tokens_in=usage.get("input", 0), tokens_out=usage.get("output", 0),
                               tokens_cached=usage.get("cached", 0))
        for k in ("proofread_calls", "proofread_lines", "proofread_skipped"):
            if k in meta:
                job.timings[k] = meta[k]

        with self._lock:
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
