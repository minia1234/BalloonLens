"""Two calls to one backend are at least MIN_CALL_GAP_S apart (plan §8)."""
import json
import time

from balloonlens.backends import page
from balloonlens.backends.page import CliTranslator
from balloonlens.cache import Cache


class Stamping:
    """Answers every line and records when each call started."""

    backend_id = "claude"
    cache_id = "claude-cli"

    def __init__(self):
        self.starts = []

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        self.starts.append(time.monotonic())
        ids = json.loads(payload)["target_ids"]
        return {"data": {"items": [{"id": sid, "text": "번역문", "review": False} for sid in ids]},
                "usage": {}, "seconds": 0}


def test_consecutive_calls_wait_for_the_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(page, "MIN_CALL_GAP_S", 0.3)
    backend = Stamping()
    translator = CliTranslator(backend, Cache(tmp_path / "c.db"))
    for line in ("待って", "またね"):
        translator.translate_page([(1, line)], "ja", "ko", "claude-haiku-5-5", "scope", lambda *args: None)
    assert len(backend.starts) == 2
    assert backend.starts[1] - backend.starts[0] >= 0.29
