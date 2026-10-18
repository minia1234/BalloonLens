"""Loopback HTTP API for the extension (section 14.1)."""
import base64
import binascii
import json
import re
import secrets
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config, memory
from . import layout as layouts
from .backends.base import BackendError
from .cache import bbox_key
from .jobs import QueueFull
from .ocr import ImageTooLarge
from .translate import LANG_NAMES, ModelPuller, TranslateError, ollama_tags

SRC_LANGS = {"ja", "en"}
READINGS = {"rtl", "ltr", "ttb"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^\d{1,6},\d{1,6},\d{1,6},\d{1,6}$")
MAX_LINE_CHARS = 2000
MAX_COORD = 30000
SUBSCRIPTION_BACKENDS = ("claude",)
# A page can also come as one frame: the content type below, a 4-byte length, the options JSON, then the image bytes.
PAGE_FRAME_TYPE = "application/x-balloonlens-page"
PAGE_OPTIONS_MAX = 64_000


def check_box(box):
    if box is None:
        return box
    if len(box) != 4 or not all(0 <= v <= MAX_COORD for v in box) or box[2] < 4 or box[3] < 4:
        raise ValueError("bad_box")
    return box


class ContextLine(BaseModel):
    source: str = Field(max_length=500)
    translation: str = Field(max_length=500)
    confirmed: bool = False


class WorkRef(BaseModel):
    page_url: str = Field(default='', max_length=1000)
    position: int = Field(default=0, ge=0, le=10000)
    memory: bool = False


class PageOptions(BaseModel):
    instructions: str = Field(default='', max_length=2000)
    glossary: str = Field(default='', max_length=4000)
    context: list[ContextLine] = Field(default_factory=list, max_length=16)
    vision: bool = False
    work: WorkRef = Field(default_factory=WorkRef)
    # None = the backend's own default: selective for the local model, off for subscription calls.
    review: Literal['off', 'selective', 'all'] | None = None
    # The part of the page the reader sees, as fractions of the image height: [top, bottom]. Balloons there are read first.
    visible: list[float] | None = Field(default=None, min_length=2, max_length=2)


class AddedRegion(BaseModel):
    uid: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    bbox: list[int]
    frame: list[int] | None = None
    moved_from: str | None = None  # bbox key this region replaced in this save; its edits follow it

    @field_validator('bbox', 'frame')
    @classmethod
    def valid_box(cls, box):
        return check_box(box)

    @field_validator('moved_from')
    @classmethod
    def valid_origin(cls, key):
        if key is not None and not KEY_RE.match(key):
            raise ValueError("bad_key")
        return key


class LayoutIn(BaseModel):
    removed: list[str] = Field(default_factory=list, max_length=500)
    added: list[AddedRegion] = Field(default_factory=list, max_length=200)
    frames: dict[str, list[int]] = Field(default_factory=dict, max_length=500)

    @field_validator('removed')
    @classmethod
    def valid_keys(cls, keys):
        if not all(KEY_RE.match(k) for k in keys):
            raise ValueError("bad_key")
        return sorted(set(keys))

    @field_validator('frames')
    @classmethod
    def valid_frames(cls, frames):
        for key, box in frames.items():
            if not KEY_RE.match(key):
                raise ValueError("bad_key")
            check_box(box)
        return frames


class WorkIn(BaseModel):
    title: str = Field(default='', max_length=120)
    summary: str = Field(default='', max_length=400)
    characters: str = Field(default='', max_length=800)


class PageBody(BaseModel):
    image: str
    options: PageOptions = Field(default_factory=PageOptions)


class LineIn(BaseModel):
    text: str
    src: str
    tgt: str
    model: str = config.DEFAULT_MODEL
    backend: Literal['claude', 'ollama'] | None = None
    scope: str = ""
    fresh: bool = False
    options: PageOptions = Field(default_factory=PageOptions)
    page: list[ContextLine] = Field(default_factory=list, max_length=100)


class EditIn(BaseModel):
    image_hash: str
    src: str
    tgt: str
    bbox: list[int]
    src_text: str | None = None  # "" removes a source correction
    tgt_text: str | None = None  # "" removes a translation edit
    clear: bool = False
    scope: str = Field(default='', max_length=200)


def resolve_model(backend: str | None, model: str) -> str:
    """The backend field wins over the model field when both are sent (plan §16.4)."""
    if backend is None:
        return model
    if backend == 'ollama':
        return model if model in config.LOCAL_MODELS else config.DEFAULT_MODEL
    return config.CLAUDE_MODEL


def check_scope(scope: str) -> str:
    if not re.match(r'^https?://', scope or '') or len(scope) > 200:
        raise HTTPException(400, "bad_scope")
    return scope


def check_hash(image_hash: str, src: str):
    if not HASH_RE.match(image_hash) or src not in SRC_LANGS:
        raise HTTPException(400, "bad_params")


def check_pair(src: str, tgt: str, model: str):
    if src not in SRC_LANGS or tgt not in LANG_NAMES or tgt == src:
        raise HTTPException(400, "unsupported_language_pair")
    if model not in config.SUPPORTED_MODELS:
        raise HTTPException(400, "unsupported_model")


def _backend_error(code: str, status: int = 409):
    return HTTPException(status, code)


def _local_complete(chat, model):
    """Structured JSON from the local Ollama model (used for work-memory suggestions)."""
    def complete(system, user, schema):
        content, _ = chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                          fmt=schema, think=False)
        try:
            return json.loads(content)
        except ValueError:
            return None
    return complete


def create_app(manager, token: str, port: int = config.PORT, tags=ollama_tags,
               extension_ids=config.EXTENSION_IDS, puller: ModelPuller | None = None) -> FastAPI:
    app = FastAPI(title="BalloonLens helper", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    pair_origins = {f"chrome-extension://{i}" for i in extension_ids}
    puller = puller or ModelPuller()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS-rebinding guard, then origin, then token.
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "bad_host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.url.path == "/v1/pair":
            # Browsers set Origin themselves, so web pages and other extensions can't obtain the token.
            if request.method != "POST" or origin not in pair_origins:
                return JSONResponse({"error": "pair_forbidden"}, status_code=403)
            return await call_next(request)
        if origin and not origin.startswith("chrome-extension://"):
            return JSONResponse({"error": "bad_origin"}, status_code=403)
        if not secrets.compare_digest(request.headers.get("x-balloonlens-token", ""), token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def router():
        return getattr(manager, "router", None)

    def require_backend(name: str):
        if name not in SUBSCRIPTION_BACKENDS or router() is None:
            raise HTTPException(404, "not_found")
        return router()

    @app.post("/v1/pair")
    def pair():
        return {"token": token}

    @app.get("/v1/status")
    def status():
        installed = tags()
        r = router()
        states = {b["id"]: b["state"] for b in r.summary()} if r else {}
        models = []
        for m in config.SUPPORTED_MODELS:
            if m in config.SUBSCRIPTION_MODELS:
                name = "claude"
                models.append({"name": m, "installed": states.get(name) == "ready", "kind": "subscription"})
            else:
                models.append({"name": m, "installed": installed is not None and m in installed, "kind": "local"})
        return {
            "ok": True,
            "version": config.VERSION,
            "ocr_version": config.OCR_VERSION,
            "ocr_ready": getattr(manager.ocr, "ready", True),
            "models": models,
            "default_model": config.DEFAULT_MODEL,
            "ollama": installed is not None,
            "queue": manager.queue_depth(),
            "backends": r.summary() if r else [],
        }

    @app.post("/v1/jobs")
    async def create_job(request: Request, src: str, tgt: str = "ko", model: str = config.DEFAULT_MODEL,
                         reading: str = "rtl", priority: int = 2, session: str = "", scope: str = "",
                         backend: Literal['claude', 'ollama'] | None = None):
        model = resolve_model(backend, model)
        check_pair(src, tgt, model)
        if reading not in READINGS or not 1 <= priority <= 4:
            raise HTTPException(400, "bad_params")
        content_type = request.headers.get('content-type', '')
        is_json = content_type.startswith('application/json')
        framed = content_type.startswith(PAGE_FRAME_TYPE)
        if is_json:
            limit = config.MAX_IMAGE_BYTES * 4 // 3 + 100_000
        elif framed:
            limit = config.MAX_IMAGE_BYTES + PAGE_OPTIONS_MAX + 4
        else:
            limit = config.MAX_IMAGE_BYTES
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            raise HTTPException(400, 'bad_image')
        if length > limit:
            raise HTTPException(413, "image_too_large")
        chunks, received = [], 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise HTTPException(413, 'image_too_large')
            chunks.append(chunk)
        body = b''.join(chunks)
        page_options = PageOptions()
        if is_json:
            try:
                envelope = PageBody.model_validate_json(body)
                body = base64.b64decode(envelope.image, validate=True)
                page_options = envelope.options
            except (ValidationError, ValueError, binascii.Error):
                raise HTTPException(400, 'bad_page')
        elif framed:
            # Frame: 4-byte big-endian length, the options as JSON, then the image bytes. No Base64 on either side.
            if len(body) < 4:
                raise HTTPException(400, 'bad_page')
            size = int.from_bytes(body[:4], 'big')
            if size > PAGE_OPTIONS_MAX or 4 + size > len(body):
                raise HTTPException(400, 'bad_page')
            try:
                page_options = PageOptions.model_validate_json(body[4:4 + size])
            except ValidationError:
                raise HTTPException(400, 'bad_page')
            body = body[4 + size:]
        if not body or len(body) > config.MAX_IMAGE_BYTES:
            raise HTTPException(400 if not body else 413, "bad_image")
        try:
            job = manager.submit(body, src=src, tgt=tgt, model=model, reading=reading, priority=priority,
                                 session=session[:64], scope=scope[:200], options=page_options.model_dump())
        except QueueFull:
            raise HTTPException(429, "queue_full")
        return {"id": job.id}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str):
        snap = manager.get(job_id)
        if snap is None:
            raise HTTPException(404, "not_found")
        return snap

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        if not manager.cancel(job_id):
            raise HTTPException(404, "not_found")
        return {"ok": True}

    @app.post("/v1/jobs/{job_id}/priority")
    def set_priority(job_id: str, value: int):
        if not 1 <= value <= 4:
            raise HTTPException(400, "bad_params")
        if not manager.set_priority(job_id, value):
            raise HTTPException(404, "not_found")
        return {"ok": True}

    @app.post("/v1/sessions/{session}/cancel")
    def cancel_session(session: str):
        return {"cancelled": manager.cancel_session(session)}

    @app.post("/v1/translate-line")
    def translate_line(body: LineIn):
        body = body.model_copy(update={"model": resolve_model(body.backend, body.model)})
        """Re-translate one (possibly corrected) line on demand (section 3.4)."""
        check_pair(body.src, body.tgt, body.model)
        text = body.text.strip()
        if not text or len(text) > MAX_LINE_CHARS:
            raise HTTPException(400, "bad_text")
        r = router()
        if r is not None and r.is_subscription(body.model):
            blocked = r.gate(body.model)
            if blocked:
                return {"text": None, "status": "failed", "error": blocked, "quality_errors": []}
        result = {"text": None, "status": "failed"}

        def on_result(_sid, out, st):
            result.update(text=out, status=st)

        # Preserve surrounding page text (and the work's memory) during a one-line correction.
        options = body.options.model_dump()
        options['vision'] = False
        options['context'] = ([line.model_dump() for line in body.page] + options['context'])[-16:]
        inputs = memory.translation_inputs(manager.cache, body.scope[:200], body.src, body.tgt, body.model, options)
        translator = r.translator_for(body.model) if r is not None else manager.translator
        meta = translator.translate_page(
            [(0, text)], body.src, body.tgt, body.model, body.scope[:200], on_result, use_cache=not body.fresh,
            context=inputs['context'], instructions=inputs['instructions'], glossary=inputs['glossary'],
            notes=inputs['notes'], review=options['review'])
        result["error"] = meta.get("error")
        result['quality_errors'] = meta.get('quality_errors', [])
        return result

    @app.put("/v1/edits")
    def put_edit(body: EditIn):
        if not HASH_RE.match(body.image_hash) or len(body.bbox) != 4:
            raise HTTPException(400, "bad_params")
        if body.src not in SRC_LANGS or body.tgt not in LANG_NAMES:
            raise HTTPException(400, "unsupported_language_pair")
        cache, h = manager.cache, body.image_hash
        src_text = body.src_text.strip()[:MAX_LINE_CHARS] if body.src_text is not None else None
        tgt_text = body.tgt_text.strip()[:MAX_LINE_CHARS] if body.tgt_text is not None else None
        if body.clear:
            cache.put_edit(h, body.src, "", body.bbox, None)
            cache.put_edit(h, body.src, body.tgt, body.bbox, None)
        else:
            if src_text is not None:
                cache.put_edit(h, body.src, "", body.bbox, src_text or None)
            if tgt_text is not None:
                cache.put_edit(h, body.src, body.tgt, body.bbox, tgt_text or None)
        if body.scope:
            # Later pages of the work should see the reader's confirmed wording.
            cache.update_page_line(body.scope, h, body.src, body.tgt, bbox_key(body.bbox),
                                   src_text, tgt_text, body.clear)
        return {"ok": True}

    @app.get("/v1/layouts/{image_hash}")
    def get_layout(image_hash: str, src: str):
        check_hash(image_hash, src)
        return manager.cache.get_layout(image_hash, src) or layouts.empty()

    @app.put("/v1/layouts/{image_hash}")
    def put_layout(image_hash: str, src: str, body: LayoutIn):
        check_hash(image_hash, src)
        data = body.model_dump()
        moved = 0
        for region in data["added"]:
            origin = region.pop("moved_from", None)
            if origin:
                moved += manager.cache.move_edits(image_hash, src, origin, region["bbox"])
        manager.cache.put_layout(image_hash, src, None if layouts.is_empty(data) else data)
        return {"ok": True, "moved_edits": moved}

    @app.delete("/v1/layouts/{image_hash}")
    def delete_layout(image_hash: str, src: str):
        check_hash(image_hash, src)
        manager.cache.put_layout(image_hash, src, None)
        return {"ok": True}

    @app.get("/v1/works")
    def get_work(scope: str):
        check_scope(scope)
        return {"work": manager.cache.get_work(scope) or WorkIn().model_dump(),
                "pages": manager.cache.page_stats(scope)}

    @app.put("/v1/works")
    def put_work(scope: str, body: WorkIn):
        check_scope(scope)
        manager.cache.put_work(scope, {k: v.strip() for k, v in body.model_dump().items()})
        return {"ok": True}

    @app.delete("/v1/works")
    def delete_work(scope: str):
        check_scope(scope)
        manager.cache.delete_work(scope)
        return {"ok": True}

    @app.delete("/v1/works/pages")
    def clear_work_pages(scope: str):
        check_scope(scope)
        manager.cache.clear_pages(scope)
        return {"ok": True}

    @app.post("/v1/works/suggest")
    def suggest_work(scope: str, model: str = "qwen3.5:9b", src: str = "ja", tgt: str = "ko"):
        check_scope(scope)
        check_pair(src, tgt, model)
        r = router()
        if r is not None and r.is_subscription(model):
            blocked = r.gate(model)
            if blocked:
                raise _backend_error(blocked)
            translator = r.translator_for(model)

            def complete(system, user, schema):
                res = translator.call_json(system, user.encode("utf-8"), model=model, lines=0, schema_name="suggest",
                                           schema_json=json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
                return res["data"]
        elif model.startswith("qwen3"):
            complete = _local_complete(manager.translator.chat, model)
        else:
            raise HTTPException(400, "unsupported_model")
        try:
            return memory.suggest(manager.cache, complete, scope, src, tgt)
        except BackendError as e:
            raise _backend_error(e.code)
        except TranslateError as e:
            raise HTTPException(502, str(e)[:200])

    @app.post("/v1/models/pull")
    def pull_model(name: str):
        if name not in config.LOCAL_MODELS:
            raise HTTPException(400, "unsupported_model")
        puller.start(name)
        return puller.get(name)

    @app.get("/v1/models/pull")
    def pull_status(name: str):
        if name not in config.LOCAL_MODELS:
            raise HTTPException(400, "unsupported_model")
        return puller.get(name)

    # ----- subscription backends (v0.5.0) -----
    @app.get("/v1/backends")
    def list_backends():
        r = router()
        return {"backends": r.summary() if r else [],
                "local": {"ollama": tags() is not None, "models": config.LOCAL_MODELS}}

    @app.post("/v1/backends/{name}/check")
    def check_backend(name: str):
        r = require_backend(name)
        st = r.status(name, refresh=True)
        return {"status": st.as_dict(), "backend": next(b for b in r.summary() if b["id"] == name)}

    @app.post("/v1/backends/{name}/resume")
    def resume_backend(name: str):
        r = require_backend(name)
        r.quota.resume(name)
        return {"ok": True}

    @app.get("/v1/usage")
    def usage():
        r = router()
        if r is None:
            return {"backends": {}}
        return {"backends": {b["id"]: {"subscription_usage": b["subscription_usage"], "paused": b["paused"]}
                             for b in r.summary()}}

    @app.exception_handler(ImageTooLarge)
    async def too_large(_, exc):
        return JSONResponse({"error": "image_too_large"}, status_code=413)

    return app
