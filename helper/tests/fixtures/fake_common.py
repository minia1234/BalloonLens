"""Shared pieces of the fake claude and codex CLIs. Test-only, never shipped.

Leading options are for the test harness and are removed before the real flags are read:
--mode NAME picks the behaviour, --log PATH appends one JSON line per translation call.
"""
import json
import os

SECRET_LOOKALIKE = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE"


def take_options(argv):
    mode, log = "ok", None
    while len(argv) >= 2 and argv[0] in ("--mode", "--log"):
        if argv[0] == "--mode":
            mode = argv[1]
        else:
            log = argv[1]
        argv = argv[2:]
    return mode, log, argv


def record(log, argv, stdin: bytes):
    if not log:
        return
    row = {"argv": argv, "stdin": stdin.decode("utf-8", "replace"), "env": sorted(os.environ)}
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def data_body(stdin: bytes) -> dict:
    """The JSON page data between the DATA markers that codex_cli.complete_json writes."""
    inner = stdin.split(b"<DATA>\n", 1)[1].rsplit(b"\n</DATA>", 1)[0]
    return json.loads(inner.decode("utf-8"))


def reply_items(mode: str, body: dict) -> list:
    """Translations for the target ids. The modes shape what a misbehaving model would return."""
    ids = list(body.get("target_ids", []))
    if mode == "missing" and ids:
        ids = ids[:-1]
    text = "교정문" if body.get("drafts") else "번역문"
    items = [{"id": sid, "text": text, "review": False} for sid in ids]
    if items and mode == "secret":
        items[0]["text"] = SECRET_LOOKALIKE
    if items and mode == "foreign_url":
        items[0]["text"] = "번역문 https://evil.example/steal"
    if items and mode == "meta":
        items[0]["text"] = "다음은 번역입니다: 번역문"
    return items
