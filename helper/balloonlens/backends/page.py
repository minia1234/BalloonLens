"""Page translation through a subscription CLI: chunks, one call per chunk, checks, optional review pass.

Callers see the same contract as the local Translator: on_result(id, text_or_None, status) per line, and
a meta dict back. Nothing here sends images, and every reply passes the checks in validate.page_reasons.

Calls run side by side: up to config.CLI_PARALLEL at once for one backend, shared by all pages. The chunks of one
page go out together, and each still sees the whole page's source text. Only a single call at a time
(CLI_PARALLEL=1) lets a later chunk also see the last lines translated before it.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..cache import tr_key
from . import prompts, validate
from .base import BackendError, CallCancelled, cancel_scope, digest

MAX_CONTEXT_ITEMS = 32
MAX_CONTEXT_CHARS = 3000


def _bounded_context(context) -> list[dict]:
    """Keeps the nearest lines (the end of the list) within a character budget."""
    items = []
    for c in (context or [])[-MAX_CONTEXT_ITEMS:]:
        if not isinstance(c, dict):
            continue
        item = {"source": str(c.get("source", ""))[:500], "translation": str(c.get("translation", ""))[:500]}
        if c.get("confirmed"):
            item["confirmed"] = True
        items.append(item)
    kept, total = [], 0
    for item in reversed(items):
        total += len(item["source"]) + len(item["translation"])
        if total > MAX_CONTEXT_CHARS:
            break
        kept.append(item)
    return list(reversed(kept))


def _items(data: dict, ids) -> dict:
    wanted = set(ids)
    out = {}
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        if type(sid) is not int or sid not in wanted or sid in out:
            continue
        raw = item.get("text")
        text = validate.clean_output(raw) if isinstance(raw, str) else ""
        out[sid] = {"text": text or None, "review": bool(item.get("review")),
                    "reason": str(item.get("reason") or "")[:120]}
    return out


def _add_usage(total: dict, usage: dict):
    for key in ("input", "output", "cached"):
        total[key] = total.get(key, 0) + int(usage.get(key, 0))


TIMING_KEYS = ("cli_ms", "api_ms", "slot_wait_ms", "gap_wait_ms", "warm_calls")  # summed per page, see _call


def _new_meta() -> dict:
    return {"calls": 0, "usage": {"input": 0, "output": 0, "cached": 0}, "network_errors": [],
            **{key: 0 for key in TIMING_KEYS}}


def _merge_meta(meta: dict, part: dict):
    """Adds one chunk's counters to the page's meta. Chunks may run in threads, so each keeps its own counters."""
    meta["calls"] += part["calls"]
    _add_usage(meta["usage"], part["usage"])
    meta["network_errors"].extend(part["network_errors"])
    for key in TIMING_KEYS:
        meta[key] += part[key]
    if part.get("error"):
        meta["error"] = part["error"]
    if part.get("fatal") and not meta.get("fatal"):
        meta["fatal"] = part["fatal"]
    for key in ("proofread_calls", "proofread_lines", "proofread_skipped"):
        if key in part:
            meta[key] = meta.get(key, 0) + part[key]


MIN_CALL_GAP_S = 1.0  # at least one second between the starts of two CLI calls to one backend (plan §8)


class CliTranslator:
    def __init__(self, backend, cache, quota=None, *, chunk_size=prompts.MAX_LINES_PER_CALL, parallel=None):
        self.backend = backend
        self.cache = cache
        self.quota = quota
        self.chunk_size = chunk_size
        self.parallel = max(1, int(parallel or config.CLI_PARALLEL))
        self.min_gap = MIN_CALL_GAP_S
        self._slots = threading.BoundedSemaphore(self.parallel)  # CLI calls running at once
        self._gate = threading.Lock()  # guards the start schedule and the in-flight count
        self._next_start = 0.0  # monotonic time before which no call may start
        self._inflight = 0

    def prepare(self, src, tgt, model, *, instructions=""):
        """Prepare the same prompt the page will use, without making a translation call."""
        prepare = getattr(self.backend, "prepare", None)
        if prepare is not None:
            prepare(prompts.system_prompt(src, tgt, instructions), model=model)

    def _record(self, model, lines, usage, seconds, outcome, retry_after=None):
        if self.quota is not None:
            self.quota.record(backend=self.backend.backend_id, model=model, lines=lines, usage=usage,
                              seconds=seconds, outcome=outcome, retry_after=retry_after)

    def call_json(self, system, payload, *, model, schema_json=None, schema_name="page",
                  lines=0, should_cancel=None):
        """One guarded CLI call: actual-limit pause check, start spacing, the call, then the usage record. Translation and
        suggestions use it.

        A call holds one of `parallel` slots while it runs and reserves its start time at least min_gap after the
        previous start. A call paused by Claude's limit starts no process and is not counted.

        should_cancel is checked before the quota check and again after the start-gap wait, so a page cancelled while
        it waited never starts a process. A process that was running when its page was cancelled is stopped by the
        runner and recorded as "cancelled" (it may have used quota)."""
        cancelled = should_cancel or (lambda: False)
        t_slot = time.monotonic()
        with self._slots:
            slot_wait = time.monotonic() - t_slot
            with self._gate:
                if cancelled():
                    raise CallCancelled()
                if self.quota is not None:
                    self.quota.check(self.backend.backend_id, pending=self._inflight)
                now = time.monotonic()
                start_at = max(now, self._next_start)
                self._next_start = start_at + self.min_gap
                self._inflight += 1
            try:
                gap_wait = max(0.0, start_at - now)
                if gap_wait:
                    time.sleep(gap_wait)
                if cancelled():  # the page was cancelled while it waited for the start gap
                    raise CallCancelled()
                started = time.time()
                try:
                    with cancel_scope(should_cancel):
                        res = self.backend.complete_json(system, payload, model=model, schema_json=schema_json,
                                                         schema_name=schema_name)
                except BackendError as e:
                    if cancelled():
                        self._record(model, lines, None, round(time.time() - started, 2), "cancelled")
                        raise CallCancelled() from e
                    self._record(model, lines, None, round(time.time() - started, 2), e.code, e.retry_after)
                    raise
                self._record(model, lines, res["usage"], res["seconds"], "ok")
                return dict(res, slot_wait_ms=round(slot_wait * 1000), gap_wait_ms=round(gap_wait * 1000))
            finally:
                with self._gate:
                    self._inflight -= 1

    def _call(self, meta, model, system, body, ids, should_cancel):
        """One CLI call with at most one retry for format problems. Returns (items, fatal_code_or_None).

        items is None when the call failed; the caller then leaves those lines unrepaired (plan §8).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                res = self.call_json(system, prompts.encode_payload(body), model=model, lines=len(ids),
                                     should_cancel=should_cancel)
            except BackendError as e:
                if e.code == "cancelled":  # not a failure: the page no longer wants the answer
                    return None, None
                meta["error"] = e.code
                meta["network_errors"].append(e.code)
                if e.fatal:
                    meta["fatal"] = e.code
                    return {}, e.code
                if e.retryable and attempts < 2 and not should_cancel():
                    continue
                return None, None
            meta["calls"] += 1
            _add_usage(meta["usage"], res["usage"])
            meta["cli_ms"] += round(res["seconds"] * 1000)
            meta["api_ms"] += round(res.get("api_ms") or 0)  # the CLI reports it; 0 when it does not
            meta["slot_wait_ms"] += res["slot_wait_ms"]
            meta["gap_wait_ms"] += res["gap_wait_ms"]
            meta["warm_calls"] += 1 if res.get("warm") else 0  # calls answered by a process started ahead of time
            return _items(res["data"], ids), None

    def translate_page(self, lines, src, tgt, model, scope, on_result, should_cancel=lambda: False,
                       use_cache=True, *, context=None, instructions="", glossary="", fixed=None, image=None,
                       notes="", uncertain=(), review=None) -> dict:
        review = review or "off"
        meta = {"calls": 0, "load_ms": 0, "cache_hits": 0, "quality_errors": [], "network_errors": [],
                "usage": {"input": 0, "output": 0, "cached": 0}, "review": review,
                "backend": self.backend.backend_id, **{key: 0 for key in TIMING_KEYS}}
        if image is not None:
            meta["image_ignored"] = True  # subscription backends never receive pixels (design decision D1)

        page = [(sid, text) for sid, text in lines]
        text_of = dict(page)
        fixed = {sid: text for sid, text in (fixed or {}).items() if sid in text_of}
        uncertain = set(uncertain) & set(text_of)
        context = _bounded_context(context)
        terms = validate.glossary_terms(glossary)
        term_dicts = [{"source": s, "target": t} for s, t in terms]
        casual = "반말" in instructions and "존댓말" not in instructions
        # The lines translated before this page are not part of the key: the same lines must reuse their translation
        # when the reader comes back and the earlier pages differ (D58). Context still reaches the prompt below.
        reference = {"page": page, "fixed": sorted(fixed.items()),
                     "instructions": instructions, "glossary": glossary, "notes": notes,
                     "uncertain": sorted(uncertain), "review": review}
        signature = digest(json.dumps(reference, ensure_ascii=False, sort_keys=True, default=str))
        cache_model = f"{self.backend.cache_id}:{model}"
        system = prompts.system_prompt(src, tgt, instructions)
        proof_system = prompts.system_prompt(src, tgt, instructions, proofread=True)

        reported = set()
        keys, pending = {}, []
        for sid, text in page:
            if should_cancel():
                return meta
            if sid in fixed:
                on_result(sid, fixed[sid], "done")
                reported.add(sid)
                continue
            key = tr_key(json.dumps([signature, sid]), src, tgt, cache_model, config.CLI_PROMPT_VERSION, scope)
            hit = self.cache.get_tr(key) if use_cache else None
            if hit is not None:
                meta["cache_hits"] += 1
                on_result(sid, hit, "done")
                reported.add(sid)
            else:
                keys[sid] = key
                pending.append(sid)

        def proofread(part, chunk, results, ctx):
            cands = []
            for sid in chunk:
                text = results[sid]["text"]
                if text is None:
                    continue
                flags = ["model_uncertain"] if "model_uncertain" in results[sid]["reasons"] else []
                if review == "all" or validate.review_reasons(text_of[sid], text, terms=terms, casual=casual,
                                                              uncertain=sid in uncertain, flags=flags):
                    cands.append(sid)
            counted = sum(1 for sid in chunk if results[sid]["text"] is not None)
            part["proofread_skipped"] = part.get("proofread_skipped", 0) + counted - len(cands)
            if not cands:
                return None
            part["proofread_calls"] = part.get("proofread_calls", 0) + 1
            part["proofread_lines"] = part.get("proofread_lines", 0) + len(cands)
            body = prompts.build_payload(src, tgt, page, cands, context=ctx, glossary=term_dicts, notes=notes,
                                         fixed=fixed, uncertain=uncertain & set(cands),
                                         drafts={sid: results[sid]["text"] for sid in cands})
            better, fatal = self._call(part, model, proof_system, body, cands, should_cancel)
            better = better or {}
            for sid in cands:
                item = better.get(sid) or {}
                text = item.get("text")
                if text is None:
                    results[sid]["reasons"].append("proofread_unavailable")
                    continue
                reasons = validate.page_reasons(text_of[sid], text, tgt)
                if item.get("review"):
                    reasons.append("model_uncertain")
                if sid in uncertain:
                    reasons.append("ocr_uncertain")
                results[sid] = {"text": text, "reasons": list(dict.fromkeys(reasons))}
            return fatal

        def solve(chunk, prev_tail, halt):
            """One chunk: one call, one retry for the lines it missed, then the optional review pass.

            Returns (results, part, stop). results is None when the chunk was not answered. Runs in a worker thread
            when chunks go in parallel, so it writes only to its own part."""
            part = _new_meta()
            if halt.is_set() or should_cancel():
                return None, part, None
            ctx = _bounded_context(context + prev_tail)
            body = prompts.build_payload(src, tgt, page, chunk, context=ctx, glossary=term_dicts, notes=notes,
                                         fixed=fixed, uncertain=uncertain)
            got, stop = self._call(part, model, system, body, chunk, should_cancel)
            if stop:
                return None, part, stop
            failed = got is None  # a failed call leaves no reply to repair, so its lines get no targeted retry
            got = {} if failed else got
            missing = [sid for sid in chunk if (got.get(sid) or {}).get("text") is None]
            if missing and not failed and not should_cancel():
                body2 = prompts.build_payload(src, tgt, page, missing, context=ctx, glossary=term_dicts,
                                              notes=notes, fixed=fixed, uncertain=uncertain,
                                              retry_reasons={sid: ["missing_or_empty"] for sid in missing})
                again, stop = self._call(part, model, system, body2, missing, should_cancel)
                again = again or {}
                for sid in missing:
                    if (again.get(sid) or {}).get("text") is not None:
                        got[sid] = again[sid]

            results = {}
            for sid in chunk:
                item = got.get(sid) or {}
                text = item.get("text")
                if text is None:
                    results[sid] = {"text": None, "reasons": ["missing_or_empty"]}
                    continue
                reasons = validate.page_reasons(text_of[sid], text, tgt)
                if item.get("review"):
                    reasons.append("model_uncertain")
                if sid in uncertain:
                    reasons.append("ocr_uncertain")
                results[sid] = {"text": text, "reasons": list(dict.fromkeys(reasons))}

            if review != "off" and not stop and not should_cancel():
                stop = proofread(part, chunk, results, ctx) or None
            return results, part, stop

        halt = threading.Event()  # set on a fatal error: chunks that have not started yet stay unsent

        def emit(chunk, results, part, stop):
            """Main thread only: writes one chunk's counters, results, cache entries and quality notes."""
            _merge_meta(meta, part)
            if stop:
                halt.set()
            if results is None:
                return
            for sid in chunk:
                r = results[sid]
                reported.add(sid)
                if r["text"] is None or "secret_pattern" in r["reasons"]:
                    # A reply containing secret-looking text is never shown or cached.
                    on_result(sid, None, "failed")
                    meta["quality_errors"].append({"id": sid, "reasons": r["reasons"]})
                elif r["reasons"]:
                    on_result(sid, r["text"], "review")
                    meta["quality_errors"].append({"id": sid, "reasons": r["reasons"]})
                else:
                    if use_cache:
                        self.cache.put_tr(keys[sid], r["text"])
                    on_result(sid, r["text"], "done")

        chunks = prompts.chunk_ids(pending, self.chunk_size)
        if self.parallel > 1 and len(chunks) > 1:
            pool = ThreadPoolExecutor(max_workers=min(self.parallel, len(chunks)),
                                      thread_name_prefix="balloonlens-chunk")
            try:
                owner = {pool.submit(solve, chunk, [], halt): chunk for chunk in chunks}
                for fut in as_completed(owner):
                    if should_cancel():
                        break
                    results, part, stop = fut.result()
                    emit(owner[fut], results, part, stop)
            finally:
                halt.set()
                pool.shutdown(wait=False, cancel_futures=True)  # a cancelled page does not wait for calls in flight
        else:
            prev_tail = []
            for chunk in chunks:
                if should_cancel():
                    return meta
                results, part, stop = solve(chunk, prev_tail, halt)
                emit(chunk, results, part, stop)
                if results is not None:
                    prev_tail = [{"source": text_of[sid], "translation": results[sid]["text"] or "", "confirmed": False}
                                 for sid in chunk[-prompts.CONTEXT_LINES:] if results[sid]["text"]]
                if stop:
                    break

        if should_cancel():
            return meta
        for sid in pending:
            if sid not in reported:
                on_result(sid, None, "failed")
                reported.add(sid)
        return meta
