import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_fakes import PageOcr
from balloonlens.cache import Cache
from balloonlens.jobs import JobManager
from balloonlens.ocr import decode_image

TOKEN = "t0ken"
EXT = "ourextensionid"
HOST = {"host": "127.0.0.1:8787"}
AUTH = {**HOST, "x-balloonlens-token": TOKEN}


class FakeOcr(PageOcr):
    calls = 0

    def run(self, img, src, reading):
        FakeOcr.calls += 1
        return [{"id": 0, "bbox": [1, 2, 3, 4], "src": "こんにちは", "conf": None},
                {"id": 1, "bbox": [5, 6, 7, 8], "src": "さようなら", "conf": None}], {"detect_ms": 1}


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def translate(self, lines, src, tgt, model, scope, on_result, should_cancel=lambda: False, use_cache=True):
        self.calls.append({"lines": list(lines), "model": model, "use_cache": use_cache})
        for sid, text in lines:
            on_result(sid, f"번역:{text}", "done")
        return {"load_ms": 0}

    def translate_page(self, lines, src, tgt, model, scope, on_result, **options):
        self.page_input = {'lines': list(lines), **options}
        fixed = options.get('fixed') or {}
        return self.translate([(sid, text) for sid, text in lines if sid not in fixed],
                              src, tgt, model, scope, on_result, use_cache=options.get('use_cache', True))


def png_bytes(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def setup(tmp_path):
    from balloonlens.server import create_app

    translator = FakeTranslator()
    manager = JobManager(FakeOcr(), translator, Cache(tmp_path / "c.db"), decode_image)
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["translategemma:4b"], extension_ids={EXT}))
    return manager, client, translator


def wait_done(client, job_id):
    for _ in range(100):
        snap = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if snap["status"] in ("done", "partial", "failed", "cancelled"):
            return snap
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def run_job(client, color="white"):
    r = client.post("/v1/jobs?src=ja&tgt=ko&session=s1", content=png_bytes(color), headers=AUTH)
    return wait_done(client, r.json()["id"])


def test_rejects_missing_token_bad_origin_and_bad_host(setup):
    _, client, _ = setup
    assert client.get("/v1/status", headers=HOST).status_code == 401
    assert client.get("/v1/status", headers={**AUTH, "origin": "https://evil.example"}).status_code == 403
    assert client.get("/v1/status", headers={**AUTH, "host": "evil.example:8787"}).status_code == 403
    ok = client.get("/v1/status", headers={**AUTH, "origin": "chrome-extension://abc"})
    assert ok.status_code == 200 and ok.json()["models"][0]["installed"] is True


def test_pairing_only_for_our_extension(setup):
    _, client, _ = setup
    assert client.post("/v1/pair", headers={**HOST, "origin": f"chrome-extension://{EXT}"}).json() == {"token": TOKEN}
    assert client.post("/v1/pair", headers={**HOST, "origin": "chrome-extension://other"}).status_code == 403
    assert client.post("/v1/pair", headers={**HOST, "origin": "https://evil.example"}).status_code == 403
    assert client.post("/v1/pair", headers=HOST).status_code == 403
    assert client.get("/v1/pair", headers={**HOST, "origin": f"chrome-extension://{EXT}"}).status_code == 403


def test_job_flow_and_ocr_cache(setup):
    manager, client, _ = setup
    manager.start()
    FakeOcr.calls = 0
    for _ in range(2):
        snap = run_job(client)
        assert snap["status"] == "done"
        assert [s["tgt"] for s in snap["segments"]] == ["번역:こんにちは", "번역:さようなら"]
        assert (snap["width"], snap["height"]) == (40, 30) and len(snap["image_hash"]) == 64
    assert FakeOcr.calls == 1  # second identical image hit the OCR cache


def test_user_edits_survive_reprocessing_and_can_be_cleared(setup):
    manager, client, translator = setup
    manager.start()
    first = run_job(client)
    edit = {"image_hash": first["image_hash"], "src": "ja", "tgt": "ko", "bbox": [1, 2, 3, 4]}
    assert client.put("/v1/edits", json={**edit, "src_text": "こんばんは", "tgt_text": "내가 고친 번역"},
                      headers=AUTH).status_code == 200
    assert client.put("/v1/edits", json={**edit, "bbox": [5, 6, 7, 8], "src_text": "またね"}, headers=AUTH).status_code == 200

    again = run_job(client)
    s0, s1 = again["segments"]
    assert (s0["src"], s0["tgt"], s0["edited"], s0["ocr_src"]) == ("こんばんは", "내가 고친 번역", True, "こんにちは")
    assert (s1["src"], s1["tgt"], s1["edited"]) == ("またね", "번역:またね", False)  # corrected source is re-translated
    assert translator.calls[-1]["lines"] == [(1, "またね")]  # hand-edited line isn't sent to the model

    client.put("/v1/edits", json={**edit, "clear": True}, headers=AUTH)
    assert run_job(client)["segments"][0]["tgt"] == "번역:こんにちは"


def test_translate_line(setup):
    _, client, translator = setup
    r = client.post("/v1/translate-line", json={"text": " 待って ", "src": "ja", "tgt": "ko", "model": "qwen3:8b",
                                                "fresh": True}, headers=AUTH)
    assert r.json()["text"] == "번역:待って" and r.json()["status"] == "done"
    assert translator.calls[-1] == {"lines": [(0, "待って")], "model": "qwen3:8b", "use_cache": False}
    assert client.post("/v1/translate-line", json={"text": "", "src": "ja", "tgt": "ko"}, headers=AUTH).status_code == 400


def test_validation_errors(setup):
    _, client, _ = setup
    assert client.post("/v1/jobs?src=xx", content=png_bytes(), headers=AUTH).status_code == 400
    assert client.post("/v1/jobs?src=ja&model=gpt", content=png_bytes(), headers=AUTH).status_code == 400
    assert client.post("/v1/jobs?src=ja", content=b"", headers=AUTH).status_code == 400
    assert client.post("/v1/models/pull?name=evil", headers=AUTH).status_code == 400
    bad_edit = {"image_hash": "x", "src": "ja", "tgt": "ko", "bbox": [1, 2, 3, 4], "tgt_text": "a"}
    assert client.put("/v1/edits", json=bad_edit, headers=AUTH).status_code == 400


def test_cancel_session_before_worker_runs(setup):
    manager, client, _ = setup  # worker not started: jobs stay queued
    ids = [client.post("/v1/jobs?src=ja&session=tab1", content=png_bytes(), headers=AUTH).json()["id"]
           for _ in range(2)]
    other = client.post("/v1/jobs?src=ja&session=tab2", content=png_bytes(), headers=AUTH).json()["id"]
    assert client.post("/v1/sessions/tab1/cancel", headers=AUTH).json() == {"cancelled": 2}
    assert all(manager.get(i)["status"] == "cancelled" for i in ids)
    assert manager.get(other)["status"] == "queued"


def test_priority_picks_user_selection_first(setup):
    manager, client, _ = setup
    low = client.post("/v1/jobs?src=ja&priority=3", content=png_bytes(), headers=AUTH).json()["id"]
    high = client.post("/v1/jobs?src=ja&priority=1", content=png_bytes("black"), headers=AUTH).json()["id"]
    assert manager._next().id == high
    assert client.post(f"/v1/jobs/{low}/priority?value=1", headers=AUTH).status_code == 200


def test_reading_direction_does_not_reuse_wrong_ocr_order(setup):
    manager, client, _ = setup
    calls = []

    def run(img, src, reading):
        calls.append(reading)
        return [{"id": 0, "bbox": [1, 2, 3, 4], "src": reading, "conf": None}], {}

    manager.ocr.run = run
    manager.start()
    for reading in ("rtl", "ltr", "ltr"):
        response = client.post(f"/v1/jobs?src=ja&reading={reading}", content=png_bytes(), headers=AUTH)
        snap = wait_done(client, response.json()["id"])
        assert snap["segments"][0]["src"] == reading
    assert calls == ["rtl", "ltr"]


def test_page_options_and_fixed_lines_reach_translator(setup):
    import base64
    manager, client, translator = setup
    manager.start()
    first = run_job(client)
    client.put('/v1/edits', headers=AUTH, json={'image_hash': first['image_hash'], 'src': 'ja', 'tgt': 'ko',
                                               'bbox': [1, 2, 3, 4], 'tgt_text': '고친 대사'})
    r = client.post('/v1/jobs?src=ja', headers=AUTH, json={
        'image': base64.b64encode(png_bytes()).decode(),
        'options': {'instructions': '친구끼리는 반말', 'glossary': '田中=다나카',
                    'context': [{'source': '待って', 'translation': '잠깐만'}]},
    })
    assert r.status_code == 200
    snap = wait_done(client, r.json()['id'])
    assert snap['status'] == 'done'
    assert translator.page_input['lines'] == [(0, 'こんにちは'), (1, 'さようなら')]
    assert translator.page_input['fixed'] == {0: '고친 대사'}
    assert translator.page_input['instructions'] == '친구끼리는 반말'
    assert translator.page_input['context'][0]['translation'] == '잠깐만'
    assert all('presentation' in s for s in snap['segments'])


def test_page_options_are_bounded(setup):
    _, client, _ = setup
    import base64
    encoded = base64.b64encode(png_bytes()).decode()
    for body in ({'image': '!!'}, {'image': encoded, 'options': {'instructions': 'x'*2001}}):
        assert client.post('/v1/jobs?src=ja', headers=AUTH, json=body).status_code == 400


def framed(options: dict, image: bytes) -> bytes:
    import json
    import struct
    text = json.dumps(options).encode()
    return struct.pack(">I", len(text)) + text + image


def test_framed_page_carries_options_without_base64(setup):
    manager, client, translator = setup
    manager.start()
    body = framed({'instructions': '친구끼리는 반말', 'context': [{'source': '待って', 'translation': '잠깐만'}]}, png_bytes())
    r = client.post('/v1/jobs?src=ja', headers={**AUTH, 'content-type': 'application/x-balloonlens-page'}, content=body)
    assert r.status_code == 200
    assert wait_done(client, r.json()['id'])['status'] == 'done'
    assert translator.page_input['instructions'] == '친구끼리는 반말'
    assert translator.page_input['context'][0]['translation'] == '잠깐만'


def test_framed_page_rejects_bad_lengths_and_oversized_options(setup):
    import struct
    from balloonlens.server import PAGE_FRAME_TYPE, PAGE_OPTIONS_MAX
    _, client, _ = setup
    headers = {**AUTH, 'content-type': PAGE_FRAME_TYPE}
    assert client.post('/v1/jobs?src=ja', headers=headers, content=b'\x00\x00').status_code == 400
    assert client.post('/v1/jobs?src=ja', headers=headers, content=struct.pack('>I', 99) + b'{}').status_code == 400
    assert client.post('/v1/jobs?src=ja', headers=headers,
                       content=struct.pack('>I', PAGE_OPTIONS_MAX + 1) + b'x' * (PAGE_OPTIONS_MAX + 1)).status_code == 400
    assert client.post('/v1/jobs?src=ja', headers=headers, content=framed({}, b'')).status_code == 400
