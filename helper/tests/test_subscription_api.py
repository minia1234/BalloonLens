"""Helper API for the subscription backend: usage limits, the gate in front of every subscription call,
and the privacy of the metrics file."""
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_fakes import PageOcr
from balloonlens import config
from balloonlens.backends.base import BackendStatus
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.backends.router import Router
from balloonlens.cache import Cache
from balloonlens.jobs import JobManager
from balloonlens.ocr import decode_image
from balloonlens.server import create_app

TOKEN = "t0ken"
EXT = "ourextensionid"
AUTH = {"host": "127.0.0.1:8787", "x-balloonlens-token": TOKEN}
CLAUDE = "claude-haiku-5-5"
FINISHED = ("done", "partial", "failed", "cancelled")


class FakeCli:
    """A scripted subscription backend: no process starts, every call is recorded."""

    def __init__(self, backend_id, model, state="ready"):
        self.backend_id = backend_id
        self.cache_id = f"{backend_id}-cli"
        self.label = backend_id
        self.model = model
        self.state = state
        self.calls = []
    def preflight(self):
        return BackendStatus(self.backend_id, self.state, "fake", "", time.time())

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        body = json.loads(payload)
        self.calls.append(body)
        items = [{"id": sid, "text": "번역문", "review": False} for sid in body["target_ids"]]
        return {"data": {"items": items}, "usage": {"input": 1, "output": 1, "cached": 0}, "seconds": 0.01}



class Ocr(PageOcr):
    def run(self, img, src, reading):
        return [{"id": 0, "bbox": [1, 2, 3, 4], "src": "こんにちは", "conf": None},
                {"id": 1, "bbox": [5, 6, 7, 8], "src": "さようなら", "conf": None}], {"detect_ms": 1}


class LocalStub:
    def translate_page(self, *args, **kwargs):
        raise AssertionError("the local translator must not run for a subscription model")


@pytest.fixture
def setup(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    backends = {"claude": FakeCli("claude", CLAUDE)}
    cli = {name: CliTranslator(backend, cache, quota) for name, backend in backends.items()}
    router = Router(local=LocalStub(), cli=cli, backends=backends, quota=quota, cache=cache)
    manager = JobManager(Ocr(), LocalStub(), cache, decode_image, metrics_path=tmp_path / "metrics.jsonl",
                         router=router)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"], extension_ids={EXT}))
    return client, backends, router, cache, tmp_path


def png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return buf.getvalue()


def wait_job(client, job_id):
    for _ in range(200):
        snap = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if snap["status"] in FINISHED:
            return snap
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def wait_metrics(path):
    for _ in range(100):
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        time.sleep(0.05)
    raise AssertionError("no metrics row was written")


def start_job(client, model, body=None):
    return client.post(f"/v1/jobs?src=ja&tgt=ko&model={model}", content=body or png(), headers=AUTH).json()["id"]


# ---------- the gate in front of every subscription call ----------

def test_job_runs_once_login_is_ready(setup):
    client, backends = setup[0], setup[1]
    snap = wait_job(client, start_job(client, CLAUDE))
    assert snap["status"] == "done"
    assert [s["tgt"] for s in snap["segments"]] == ["번역문", "번역문"]
    assert len(backends["claude"].calls) == 1


def test_metrics_hold_counts_not_dialogue(setup):
    client, _, _, _, tmp_path = setup
    wait_job(client, start_job(client, CLAUDE))
    rows = wait_metrics(tmp_path / "metrics.jsonl")
    text = json.dumps(rows, ensure_ascii=False)
    assert "こんにちは" not in text and "번역문" not in text
    assert rows[-1]["segments"] == 2 and rows[-1]["model"] == CLAUDE


def test_translate_line_is_refused_when_the_cli_is_not_logged_in(setup):
    client, backends = setup[0], setup[1]
    backends["claude"].state = "not_logged_in"
    body = {"text": "待って", "src": "ja", "tgt": "ko", "model": CLAUDE}
    r = client.post("/v1/translate-line", json=body, headers=AUTH).json()
    assert r["status"] == "failed" and r["error"] == "not_logged_in"
    assert backends["claude"].calls == []


# ---------- limits and pauses ----------

def test_caps_are_checked_and_shown(setup):
    client = setup[0]
    assert client.put("/v1/backends/claude/caps", json={"per_5h": 5, "per_7d": 3}, headers=AUTH).status_code == 400
    r = client.put("/v1/backends/claude/caps", json={"per_5h": 4, "per_7d": 9}, headers=AUTH)
    assert r.status_code == 200 and r.json()["caps"] == {"per_5h": 4, "per_7d": 9}
    usage = client.get("/v1/usage", headers=AUTH).json()["backends"]["claude"]
    assert usage["caps"] == {"per_5h": 4, "per_7d": 9}


def test_calibration_checks_backend_and_range(setup):
    client = setup[0]
    assert client.put("/v1/usage/calibration", json={"backend": "gpt", "percent": 10}, headers=AUTH).status_code == 422
    assert client.put("/v1/usage/calibration", json={"backend": "claude", "percent": 150},
                      headers=AUTH).status_code == 422
    r = client.put("/v1/usage/calibration", json={"backend": "claude", "percent": 12.5}, headers=AUTH)
    assert r.status_code == 200 and r.json()["estimate"] is None


def test_resume_clears_a_quota_pause(setup):
    client, _, router = setup[0], setup[1], setup[2]
    router.quota.pause("claude", 600, "test pause")
    assert client.get("/v1/usage", headers=AUTH).json()["backends"]["claude"]["paused"] is not None
    assert client.post("/v1/backends/claude/resume", headers=AUTH).status_code == 200
    assert client.get("/v1/usage", headers=AUTH).json()["backends"]["claude"]["paused"] is None


# ---------- status and checks ----------

def test_status_lists_subscription_and_local_models(setup):
    body = setup[0].get("/v1/status", headers=AUTH).json()
    kinds = {m["name"]: m["kind"] for m in body["models"]}
    assert kinds[CLAUDE] == "subscription" and kinds["qwen3.5:9b"] == "local"
    assert {b["id"] for b in body["backends"]} == {"claude"}
    assert body["ollama"] is True


def test_check_reports_the_fresh_state(setup):
    client, backends = setup[0], setup[1]
    backends["claude"].state = "not_logged_in"
    r = client.post("/v1/backends/claude/check", headers=AUTH).json()
    assert r["status"]["state"] == "not_logged_in"
    assert client.post("/v1/backends/local/check", headers=AUTH).status_code == 404


def test_model_pull_is_only_for_local_models(setup):
    assert setup[0].post(f"/v1/models/pull?name={CLAUDE}", headers=AUTH).status_code == 400


def test_backend_field_wins_over_model(setup):
    """Plan §16.4: when both are sent, the backend field decides. Here the Claude gate answers for a local model name."""
    client, backends = setup[0], setup[1]
    backends["claude"].state = "not_logged_in"
    body = {"text": "待って", "src": "ja", "tgt": "ko", "model": "qwen3.5:9b", "backend": "claude"}
    r = client.post("/v1/translate-line", json=body, headers=AUTH).json()
    assert r["status"] == "failed" and r["error"] == "not_logged_in"
    assert backends["claude"].calls == []


def test_resolve_model_maps_each_backend_to_its_model():
    """Plan §16.4: the backend field picks the model; a local name is kept only for the ollama backend."""
    from balloonlens.server import resolve_model
    assert resolve_model(None, "qwen3.5:9b") == "qwen3.5:9b"
    assert resolve_model("claude", "qwen3.5:9b") == CLAUDE
    assert resolve_model("ollama", "translategemma:4b") == "translategemma:4b"
    assert resolve_model("ollama", CLAUDE) == config.DEFAULT_MODEL
