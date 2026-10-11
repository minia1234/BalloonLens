"""Test double for the codex CLI (helper/tests only). Behaviour comes from --mode, see fake_common.

exec prints JSONL events on stdout and writes the final message to the file named by -o, as the real CLI does.
A stdin without the DATA markers is the model-based sandbox probe body; its path field names the canary file.

`sandbox -c ... -- cmd /c type <file>` stands in for the local sandbox runner (no model involved):
  sandbox_readable: every file is readable (what the read-only sandbox allowed on the test PC)
  sandbox_blocked:  files in the working folder are readable, others are refused with "Access is denied."
  sandbox_missing:  files outside the working folder are not found, so the check cannot pass
  sandbox_fails:    the sandbox does not start at all
"""
import json
import os
import sys
import time
from pathlib import Path

import fake_common

SANDBOX_MODES = ("sandbox_readable", "sandbox_blocked", "sandbox_missing", "sandbox_fails")


def sandbox_command(mode: str, argv: list) -> int:
    if mode not in SANDBOX_MODES:
        mode = "sandbox_blocked"
    if mode == "sandbox_fails":
        print("windows sandbox failed: CreateProcessWithLogonW failed: 267", file=sys.stderr)
        return 1
    target = Path(argv[-1].strip('"'))
    path = target if target.is_absolute() else Path(os.getcwd()) / target
    inside = path.resolve().parent == Path(os.getcwd()).resolve()
    if mode == "sandbox_missing" and not inside:
        print("The system cannot find the file specified.", file=sys.stderr)
        return 1
    if mode == "sandbox_blocked" and not inside:
        print("Access is denied.", file=sys.stderr)
        return 1
    if not path.exists():
        print("The system cannot find the file specified.", file=sys.stderr)
        return 1
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def probe_reply(mode: str, body: dict) -> str:
    if mode == "probe_readable":
        with open(body["path"], encoding="utf-8") as f:
            return json.dumps({"text": f.read()})
    return json.dumps({"text": ""})


def translation_reply(mode: str, stdin: bytes) -> str:
    if mode == "schema_violation":
        return "[]"
    return json.dumps({"items": fake_common.reply_items(mode, fake_common.data_body(stdin))})


def emit(event: dict):
    print(json.dumps(event))


def main() -> int:
    mode, log, argv = fake_common.take_options(sys.argv[1:])
    stdin = sys.stdin.buffer.read()
    if argv[:2] == ["login", "status"]:
        if mode == "not_logged_in":
            print("Not logged in", file=sys.stderr)
            return 1
        if mode == "api_key_login":
            print("Logged in using an API key")
            return 0
        if mode == "unknown_login":
            print("Logged in")
            return 0
        print("Logged in using ChatGPT")
        return 0
    if argv[:1] == ["sandbox"]:
        return sandbox_command(mode, argv)
    if not argv or argv[0] != "exec":
        print("unsupported command", file=sys.stderr)
        return 2
    fake_common.record(log, argv, stdin)
    out_path = argv[argv.index("-o") + 1] if "-o" in argv else ""
    emit({"type": "thread.started", "thread_id": "fake-thread"})
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "quota":
        emit({"type": "error", "message": "You've hit your usage limit. Try again in 3 hours."})
        print("ERROR: usage limit reached. Try again in 3 hours.", file=sys.stderr)
        return 1
    if mode == "auth_expired":
        emit({"type": "turn.failed", "error": {"message": "Your login has expired. Please log in again."}})
        return 1
    is_probe = b"<DATA>" not in stdin
    # probe_tool: the model-based canary tries a tool; translation calls behave normally.
    if mode == "tool" or (mode == "probe_tool" and is_probe):
        call = {"id": "c1", "type": "command_execution", "command": "type file", "aggregated_output": "",
                "exit_code": 0, "status": "completed"}
        emit({"type": "item.started", "item": dict(call, status="in_progress")})
        emit({"type": "item.completed", "item": call})
    if not is_probe:
        reply = translation_reply(mode, stdin)
    else:
        reply = probe_reply(mode, json.loads(stdin.decode("utf-8")))
    if mode == "bad":
        reply = "this is not json"
    emit({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": reply}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 200,
                                              "output_tokens": 80, "reasoning_output_tokens": 30}})
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
