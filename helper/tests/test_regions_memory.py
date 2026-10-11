import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from balloonlens import layout as layouts
from balloonlens import memory
from balloonlens.cache import Cache
from balloonlens.jobs import JobManager
from balloonlens.ocr import OcrEngine, decode_image, ocr_doubts
from balloonlens.translate import Translator, review_reasons

TOKEN = "t0ken"
AUTH = {"host": "127.0.0.1:8787", "x-balloonlens-token": TOKEN}
WORK = "https://reader.test/manga/a"


# ---------- memory ----------

def test_order_key_compares_only_like_addresses():
    assert memory.order_key("https://r.test/t/12/chapter/5?page=3#x") == [12, 5, 3]
    assert memory.is_before([12, 4, 9], [12, 5, 3]) is True
    assert memory.is_before([12, 6, 1], [12, 5, 3]) is False
    assert memory.is_before([1, 2], [12, 5, 3]) is None


def page_lines(*pairs, edited=False):
    return [{"key": f"{i},0,10,10", "src": s, "tgt": t, "status": "done", "edited": edited}
            for i, (s, t) in enumerate(pairs)]


def test_recent_context_uses_earlier_pages_only_and_marks_confirmed(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.put_page(WORK, "h1", "ja", "ko", f"{WORK}/1", 0, [1], "r", page_lines(("一", "하나")))
    cache.put_page(WORK, "h2", "ja", "ko", f"{WORK}/2", 0, [2], "r", page_lines(("田中", "다나카"), edited=True))
    cache.put_page(WORK, "h4", "ja", "ko", f"{WORK}/4", 0, [4], "r", page_lines(("未来", "미래")))
    cache.put_page("https://reader.test/manga/b", "hb", "ja", "ko", "https://reader.test/manga/b/1", 0, [1], "r",
                   page_lines(("別作品", "다른 작품")))
    context = memory.recent_context(cache, WORK, "ja", "ko", f"{WORK}/3")
    assert [c["source"] for c in context] == ["一", "田中"]  # oldest first, no later page, no other work
    assert context[1]["confirmed"] is True and context[0]["confirmed"] is False
    # The current page itself is never its own context.
    assert memory.recent_context(cache, WORK, "ja", "ko", f"{WORK}/2", exclude_hash="h2")[-1]["source"] == "一"


def test_recent_context_keeps_nearest_lines_within_budget(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.put_page(WORK, "h1", "ja", "ko", f"{WORK}/1", 0, [1], "r", page_lines(("古い" * 10, "옛" * 10)))
    cache.put_page(WORK, "h2", "ja", "ko", f"{WORK}/2", 0, [2], "r", page_lines(("近い", "가까움")))
    context = memory.recent_context(cache, WORK, "ja", "ko", f"{WORK}/3", chars=10)
    assert [c["source"] for c in context] == ["近い"]


def test_translation_inputs_memory_only_for_registered_work(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.put_work(WORK, {"title": "テスト", "summary": "두 친구 이야기", "characters": "田中 = 다나카 · 반말"})
    cache.put_page(WORK, "h1", "ja", "ko", f"{WORK}/1", 0, [1], "r", page_lines(("はい", "응")))
    on = memory.translation_inputs(cache, WORK, "ja", "ko", "qwen3.5:9b",
                                   {"work": {"memory": True, "page_url": f"{WORK}/2"}, "context": []})
    assert on["memory"] and "다나카" in on["notes"] and on["context"][0]["source"] == "はい"
    off = memory.translation_inputs(cache, WORK, "ja", "ko", "qwen3.5:9b", {"work": {"memory": False}})
    assert not off["memory"] and off["notes"] == "" and off["context"] == []
    changed = memory.translation_inputs(cache, WORK, "ja", "ko", "qwen3.5:9b",
                                        {"work": {"memory": True, "page_url": f"{WORK}/2"}, "instructions": "반말"})
    assert changed["rules"] != on["rules"]


def test_notes_are_bounded():
    notes = memory.work_notes({"characters": "\n".join(["가" * 50] * 100)})
    assert len(notes.encode("utf-8")) <= memory.NOTES_BYTES


# ---------- layout ----------

def test_apply_layout_removes_adds_and_renumbers_in_reading_order():
    ocr = [{"id": 0, "bbox": [300, 10, 20, 80], "src": "右"}, {"id": 1, "bbox": [100, 10, 20, 80], "src": "左"}]
    layout = {"removed": ["100,10,20,80"], "added": [{"uid": "u1", "bbox": [200, 10, 20, 80], "frame": [180, 10, 60, 50]}],
              "frames": {"300,10,20,80": [280, 10, 60, 40]}}
    out = layouts.apply_layout(ocr, layout, lambda bbox: ("中", None, True), "rtl")
    assert [(s["id"], s["src"]) for s in out] == [(0, "右"), (1, "中")]
    assert out[0]["origin"] == {"kind": "ocr", "key": "300,10,20,80"}
    assert out[1]["origin"] == {"kind": "user", "uid": "u1"} and out[1]["ocr_uncertain"]
    framed = layouts.apply_frames([dict(s, presentation={"kind": "overlay"}) for s in out])
    assert framed[0]["presentation"] == {"kind": "overlay", "frame": [280, 10, 60, 40], "fill": True}
    assert "user_frame" not in framed[1]


# ---------- OCR retry ----------

def test_ocr_doubts():
    assert ocr_doubts("", None, "ja") == ["empty"]
    assert ocr_doubts("……！？", None, "ja") == []
    assert "no_letters" in ocr_doubts("．．＿", None, "ja")
    assert "repeated" in ocr_doubts("ーーーーー", None, "ja")
    assert "too_short" in ocr_doubts("あ", None, "ja", capacity=8)
    assert ocr_doubts("待って", None, "ja", capacity=4) == []
    assert "low_confidence" in ocr_doubts("HELLO", 0.2, "en")


def test_doubtful_crop_is_reread_and_best_reading_kept():
    engine = OcrEngine(gpu=False)
    readings = iter(["．．．", "待ってくれ"])
    engine._manga = lambda crop: next(readings)
    img = Image.new("RGB", (120, 200), "white")
    text, conf, uncertain = engine.read_region(img, [40, 20, 30, 150], "ja")
    assert (text, uncertain) == ("待ってくれ", False)


def test_reading_that_stays_doubtful_is_marked():
    engine = OcrEngine(gpu=False)
    engine._manga = lambda crop: "ー"
    text, _, uncertain = engine.read_region(Image.new("RGB", (120, 200), "white"), [40, 20, 30, 150], "ja")
    assert uncertain and text == "ー"


# ---------- selective review ----------

def test_review_reasons():
    assert review_reasons("はい", "응") == []
    assert "number" in review_reasons("3時に", "3시에")
    assert "glossary_conflict" in review_reasons("田中です", "타나카야", terms=[("田中", "다나카")])
    assert "speech_act" in review_reasons("駅で会おう", "역에서 만나라")
    assert "question_lost" in review_reasons("本当？", "정말")
    assert "style_mismatch" in review_reasons("待って", "기다려 주세요", casual=True)
    assert "name" in review_reasons("ユウキが来た", "유키가 왔어")
    assert "ocr_uncertain" in review_reasons("はい", "응", uncertain=True)


def test_selective_review_proofreads_only_risky_lines(tmp_path):
    calls = []

    def chat(model, messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        calls.append(data)
        return json.dumps({"items": [{"id": i, "text": "응" if i == 0 else "다나카야"} for i in data["target_ids"]]}), {}

    results = {}
    meta = Translator(Cache(tmp_path / "c.db"), chat).translate_page(
        [(0, "はい"), (1, "田中だ")], "ja", "ko", "qwen3.5:9b", WORK,
        lambda i, t, s: results.__setitem__(i, (t, s)), glossary="田中 = 다나카")
    assert calls[1]["target_ids"] == [1] and calls[1]["drafts"] == [{"id": 1, "text": "다나카야"}]
    assert meta["proofread_lines"] == 1 and meta["proofread_skipped"] == 1
    assert results == {0: ("응", "done"), 1: ("다나카야", "done")}

    calls.clear()
    Translator(Cache(tmp_path / "d.db"), chat).translate_page(
        [(0, "はい"), (1, "田中だ")], "ja", "ko", "qwen3.5:9b", WORK, lambda *a: None, review="all")
    assert calls[1]["target_ids"] == [0, 1]


def test_work_notes_and_uncertain_ids_reach_the_model(tmp_path):
    calls = []

    def chat(model, messages, **kwargs):
        calls.append(json.loads(messages[-1]["content"]))
        return json.dumps({"items": [{"id": i, "text": "응"} for i in calls[-1]["target_ids"]]}), {}

    Translator(Cache(tmp_path / "c.db"), chat).translate_page(
        [(0, "はい")], "ja", "ko", "qwen3:8b", WORK, lambda *a: None, notes="Work title: X",
        uncertain={0}, context=[{"source": "a", "translation": "b", "confirmed": True}])
    assert calls[0]["work_notes"] == "Work title: X" and calls[0]["ocr_uncertain_ids"] == [0]
    assert calls[0]["context"] == [{"source": "a", "translation": "b", "confirmed": True}]


# ---------- helper API + jobs ----------

class FakeOcr:
    ready = True

    def __init__(self):
        self.regions = []

    def run(self, img, src, reading):
        return [{"id": 0, "bbox": [30, 2, 8, 20], "src": "こんにちは", "conf": None},
                {"id": 1, "bbox": [5, 2, 8, 20], "src": "さようなら", "conf": None, "ocr_uncertain": True}], {}

    def read_region(self, img, bbox, src):
        self.regions.append(bbox)
        return "追加", None, False


class RecordingTranslator:
    def __init__(self):
        self.pages = []

    def translate_page(self, lines, src, tgt, model, scope, on_result, **options):
        self.pages.append({"lines": list(lines), **options})
        fixed = options.get("fixed") or {}
        for sid, text in lines:
            on_result(sid, fixed.get(sid, f"번역:{text}"), "done")
        return {"load_ms": 0}


@pytest.fixture
def api(tmp_path):
    from balloonlens.server import create_app

    ocr, translator = FakeOcr(), RecordingTranslator()
    manager = JobManager(ocr, translator, Cache(tmp_path / "c.db"), decode_image)
    manager.start()
    client = TestClient(create_app(manager, TOKEN, port=8787, tags=lambda: ["qwen3.5:9b"]))
    return client, ocr, translator


def png(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    import base64
    return base64.b64encode(buf.getvalue()).decode()


def job(client, color="white", page=f"{WORK}/1", memory_on=True, review="selective"):
    body = {"image": png(color), "options": {"work": {"memory": memory_on, "page_url": page}, "review": review}}
    r = client.post(f"/v1/jobs?src=ja&tgt=ko&model=qwen3.5:9b&scope={WORK}", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    for _ in range(100):
        snap = client.get(f"/v1/jobs/{r.json()['id']}", headers=AUTH).json()
        if snap["status"] in ("done", "partial", "failed", "cancelled"):
            return snap
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_layout_is_applied_to_jobs_and_uncertain_ocr_is_marked(api):
    client, ocr, _ = api
    first = job(client)
    assert [s["status"] for s in first["segments"]] == ["done", "review"]  # doubtful OCR stays visible, marked
    h = first["image_hash"]
    layout = {"removed": ["5,2,8,20"], "added": [{"uid": "u1", "bbox": [16, 2, 8, 20], "frame": [10, 0, 20, 30]}],
              "frames": {"30,2,8,20": [25, 0, 15, 30]}}
    assert client.put(f"/v1/layouts/{h}?src=ja", json=layout, headers=AUTH).status_code == 200
    second = job(client)
    assert [s["src"] for s in second["segments"]] == ["こんにちは", "追加"]
    assert second["segments"][0]["presentation"]["frame"] == [25, 0, 15, 30]
    assert second["segments"][1]["origin"]["uid"] == "u1" and second["layout"]["added"][0]["uid"] == "u1"
    assert ocr.regions == [[16, 2, 8, 20]]
    job(client)
    assert ocr.regions == [[16, 2, 8, 20]]  # region OCR is cached
    assert client.delete(f"/v1/layouts/{h}?src=ja", headers=AUTH).json() == {"ok": True}
    assert len(job(client)["segments"]) == 2


def test_moving_a_region_keeps_the_readers_fixes(api):
    client, _, translator = api
    first = job(client, memory_on=False)
    h = first["image_hash"]
    edit = {"image_hash": h, "src": "ja", "tgt": "ko", "bbox": [30, 2, 8, 20], "src_text": "今日は", "tgt_text": "안녕"}
    client.put("/v1/edits", json=edit, headers=AUTH)
    moved = {"removed": ["30,2,8,20"], "added": [{"uid": "u1", "bbox": [29, 1, 10, 22], "moved_from": "30,2,8,20"}]}
    r = client.put(f"/v1/layouts/{h}?src=ja", json=moved, headers=AUTH).json()
    assert r["moved_edits"] == 2
    stored = client.get(f"/v1/layouts/{h}?src=ja", headers=AUTH).json()
    assert "moved_from" not in stored["added"][0]  # consumed at save time, never replayed
    seg = next(s for s in job(client, memory_on=False)["segments"] if s["origin"]["kind"] == "user")
    assert (seg["src"], seg["tgt"], seg["edited"]) == ("今日は", "안녕", True)
    assert translator.pages[-1]["fixed"][seg["id"]] == "안녕"  # the fixed line isn't re-translated
    bad = {"added": [{"uid": "u2", "bbox": [0, 0, 10, 10], "moved_from": "../x"}]}
    assert client.put(f"/v1/layouts/{h}?src=ja", json=bad, headers=AUTH).status_code == 422


def test_layout_validation(api):
    client, _, _ = api
    h = "a" * 64
    bad = [{"removed": ["x"]}, {"added": [{"uid": "bad id", "bbox": [0, 0, 10, 10]}]},
           {"added": [{"uid": "u", "bbox": [0, 0, 1, 10]}]}, {"frames": {"1,2,3,4": [0, 0, 10]}}]
    for body in bad:
        assert client.put(f"/v1/layouts/{h}?src=ja", json=body, headers=AUTH).status_code == 422
    assert client.put(f"/v1/layouts/nothex?src=ja", json={}, headers=AUTH).status_code == 400


def test_memory_context_reuse_and_edits_flow_across_pages(api):
    client, _, translator = api
    assert client.put(f"/v1/works?scope={WORK}", json={"title": "テスト", "characters": "田中 = 다나카"},
                      headers=AUTH).status_code == 200
    first = job(client, "white", f"{WORK}/1")
    assert translator.pages[-1]["context"] == [] and "다나카" in translator.pages[-1]["notes"]
    edit = {"image_hash": first["image_hash"], "src": "ja", "tgt": "ko", "bbox": [30, 2, 8, 20],
            "tgt_text": "안녕", "scope": WORK}
    assert client.put("/v1/edits", json=edit, headers=AUTH).status_code == 200

    job(client, "black", f"{WORK}/2")
    context = translator.pages[-1]["context"]
    assert {"source": "こんにちは", "translation": "안녕", "confirmed": True} in context

    before = len(translator.pages)
    again = job(client, "black", f"{WORK}/2")
    # Same image, same rules: the finished line is reused, not re-inferred.
    # The doubtful-OCR line (id 1) was stored as review, so it is translated again.
    assert set(translator.pages[before]["fixed"]) == {0}
    assert [text for _, text in translator.pages[before]["lines"]] == ["こんにちは", "さようなら"]
    assert again["timings"]["reused_lines"] == 1

    stats = client.get(f"/v1/works?scope={WORK}", headers=AUTH).json()
    assert stats["work"]["title"] == "テスト" and stats["pages"]["count"] == 2
    client.delete(f"/v1/works/pages?scope={WORK}", headers=AUTH)
    assert client.get(f"/v1/works?scope={WORK}", headers=AUTH).json()["pages"]["count"] == 0


def test_memory_off_does_not_store_or_read_pages(api):
    client, _, translator = api
    job(client, memory_on=False)
    assert client.get(f"/v1/works?scope={WORK}", headers=AUTH).json()["pages"]["count"] == 0
    assert translator.pages[-1]["notes"] == ""
    assert client.get("/v1/works?scope=javascript:alert(1)", headers=AUTH).status_code == 400


def test_review_mode_reaches_translator(api):
    client, _, translator = api
    job(client, review="all")
    assert translator.pages[-1]["review"] == "all"
    body = {"image": png(), "options": {"review": "sometimes"}}
    assert client.post(f"/v1/jobs?src=ja&tgt=ko&scope={WORK}", json=body, headers=AUTH).status_code == 400


def test_suggestions_are_returned_not_saved(api, monkeypatch):
    client, _, translator = api
    job(client)

    def chat(model, messages, **kwargs):
        return json.dumps({"summary": "요약", "characters": [{"name": "田中", "korean": "다나카"}],
                           "glossary": [{"source": "先輩", "target": "선배"}]}), {}

    translator.chat = chat
    r = client.post(f"/v1/works/suggest?scope={WORK}", headers=AUTH).json()
    assert r["summary"] == "요약" and r["characters"][0]["korean"] == "다나카" and r["lines"] == 2
    assert client.get(f"/v1/works?scope={WORK}", headers=AUTH).json()["work"]["summary"] == ""
