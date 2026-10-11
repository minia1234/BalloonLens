"""Page translation through a subscription CLI: chunks, one call per chunk, checks, optional review pass.

Callers see the same contract as the local Translator: on_result(id, text_or_None, status) per line, and
a meta dict back. Nothing here sends images, and every reply passes the checks in validate.page_reasons.
"""
import json
import threading
import time

from .. import config
from ..cache import tr_key
from . import prompts, validate
from .base import BackendError, digest

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


MIN_CALL_GAP_S = 1.0  # at least one second between CLI calls to one backend (plan §8)


class CliTranslator:
    def __init__(self, backend, cache, quota=None, *, chunk_size=prompts.MAX_LINES_PER_CALL):
        self.backend = backend
        self.cache = cache
        self.quota = quota
        self.chunk_size = chunk_size
        self._lock = threading.Lock()  # one CLI call at a time per backend, so caps hold under concurrent requests
        self.min_gap = MIN_CALL_GAP_S
        self._last_done = 0.0  # monotonic time when the previous call ended

    def _record(self, model, lines, usage, seconds, outcome, retry_after=None):
        if self.quota is not None:
            self.quota.record(backend=self.backend.backend_id, model=model, lines=lines, usage=usage,
                              seconds=seconds, outcome=outcome, retry_after=retry_after)

    def call_json(self, system, payload, *, model, schema_json=prompts.PAGE_SCHEMA_JSON, schema_name="page",
                  lines=0):
        """One guarded CLI call: quota check, the call, then the usage record. Translation and suggestions use it.

        The check comes first, so a blocked call starts no process and is not counted as usage."""
        with self._lock:
            if self.quota is not None:
                self.quota.check(self.backend.backend_id)
            wait = self._last_done + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            started = time.time()
            try:
                res = self.backend.complete_json(system, payload, model=model, schema_json=schema_json,
                                                 schema_name=schema_name)
            except BackendError as e:
                self._record(model, lines, None, round(time.time() - started, 2), e.code, e.retry_after)
                raise
            finally:
                self._last_done = time.monotonic()
            self._record(model, lines, res["usage"], res["seconds"], "ok")
            return res

    def _call(self, meta, model, system, body, ids, should_cancel):
        """One CLI call with at most one retry for format problems. Returns (items, fatal_code_or_None).

        items is None when the call failed; the caller then leaves those lines unrepaired (plan §8).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                res = self.call_json(system, prompts.encode_payload(body), model=model, lines=len(ids))
            except BackendError as e:
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
            return _items(res["data"], ids), None

    def translate_page(self, lines, src, tgt, model, scope, on_result, should_cancel=lambda: False,
                       use_cache=True, *, context=None, instructions="", glossary="", fixed=None, image=None,
                       notes="", uncertain=(), review=None) -> dict:
        review = review or "off"
        meta = {"calls": 0, "load_ms": 0, "cache_hits": 0, "quality_errors": [], "network_errors": [],
                "usage": {"input": 0, "output": 0, "cached": 0}, "review": review,
                "backend": self.backend.backend_id}
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
        reference = {"page": page, "context": context, "fixed": sorted(fixed.items()),
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

        def proofread(chunk, results, ctx):
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
            meta["proofread_skipped"] = meta.get("proofread_skipped", 0) + counted - len(cands)
            if not cands:
                return None
            meta["proofread_calls"] = meta.get("proofread_calls", 0) + 1
            meta["proofread_lines"] = meta.get("proofread_lines", 0) + len(cands)
            body = prompts.build_payload(src, tgt, page, cands, context=ctx, glossary=term_dicts, notes=notes,
                                         fixed=fixed, uncertain=uncertain & set(cands),
                                         drafts={sid: results[sid]["text"] for sid in cands})
            better, fatal = self._call(meta, model, proof_system, body, cands, should_cancel)
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

        stop = None
        prev_tail = []
        for chunk in prompts.chunk_ids(pending, self.chunk_size):
            if should_cancel():
                return meta
            ctx = _bounded_context(context + prev_tail)
            body = prompts.build_payload(src, tgt, page, chunk, context=ctx, glossary=term_dicts, notes=notes,
                                         fixed=fixed, uncertain=uncertain)
            got, stop = self._call(meta, model, system, body, chunk, should_cancel)
            if stop:
                break
            failed = got is None  # a failed call leaves no reply to repair, so its lines get no targeted retry
            got = {} if failed else got
            missing = [sid for sid in chunk if (got.get(sid) or {}).get("text") is None]
            if missing and not failed and not should_cancel():
                body2 = prompts.build_payload(src, tgt, page, missing, context=ctx, glossary=term_dicts,
                                              notes=notes, fixed=fixed, uncertain=uncertain,
                                              retry_reasons={sid: ["missing_or_empty"] for sid in missing})
                again, stop = self._call(meta, model, system, body2, missing, should_cancel)
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
                stop = proofread(chunk, results, ctx) or None

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
        if stop:
            meta["fatal"] = stop
        return meta
