"""Test double for the claude CLI (helper/tests only). Behaviour comes from --mode, see fake_common."""
import json
import os
import sys
import threading
import time

import fake_common


def auth_status(mode: str) -> int:
    info = {"loggedIn": True, "authMethod": "claude.ai",
            "configDirectory": os.environ.get("CLAUDE_CONFIG_DIR", ""), "subscriptionType": "pro"}
    if mode == "garbage_status":
        print("status: ok")
        return 0
    if mode == "not_logged_in":
        info.update(loggedIn=False, authMethod="none")
    elif mode == "api_key":
        info.update(authMethod="api_key")
    elif mode == "third_party":
        info.update(authMethod="third_party")
    elif mode == "dir_mismatch":
        info.update(configDirectory="C:/elsewhere")
    print(json.dumps(info))
    return 0 if info["loggedIn"] else 1


STREAM_ERRORS = {
    "quota": "Claude usage limit reached. Your limit will reset in 2 hours.",
    "not_logged_in_json": "Not logged in · Please run /login",
    "error_json": "Something broke",
}


def serve_stream(mode: str, log, argv) -> int:
    """`--input-format stream-json`: answers the start-up (initialize) request after a moment, then sends one result
    event per message line. Ends when the input closes, like the real CLI. Modes: die_idle (ends by itself when nobody
    asks it within a moment), die_mid (ends while answering), slow."""
    asked = threading.Event()
    if mode == "die_idle":
        quitter = threading.Timer(0.8, lambda: None if asked.is_set() else os._exit(0))
        quitter.daemon = True
        quitter.start()
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        if message.get("type") == "control_request":
            time.sleep(0.2)  # a moment to start
            print(json.dumps({"type": "control_response", "response": {
                "subtype": "success", "request_id": message.get("request_id")}}), flush=True)
            continue
        asked.set()
        text = message["message"]["content"]
        payload = text[:text.rindex("}") + 1].encode("utf-8")  # the data comes first, the fixed instruction last
        fake_common.record(log, argv, payload)
        if mode == "die_mid":
            print("Please run /login", file=sys.stderr, flush=True)
            return 1
        if mode == "slow":
            time.sleep(60)
        if mode in STREAM_ERRORS:
            print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": STREAM_ERRORS[mode]}),
                  flush=True)
            continue
        items = fake_common.reply_items(mode, json.loads(payload))
        usage = {"input_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 120,
                 "output_tokens": 60}
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": json.dumps({"items": items}), "usage": usage, "duration_api_ms": 50}), flush=True)
    return 0


def main() -> int:
    mode, log, argv = fake_common.take_options(sys.argv[1:])
    if argv[:2] == ["auth", "status"]:
        return auth_status(mode)
    if argv == ["--version"]:
        print("2.0.1 (Claude Code)" if mode == "old_version" else "2.1.285 (Claude Code)")
        return 0
    if "--input-format" in argv:
        return serve_stream(mode, log, argv)
    stdin = sys.stdin.buffer.read()
    fake_common.record(log, argv, stdin)
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "auth":
        print("Please run /login", file=sys.stderr)
        return 1
    if mode == "not_logged_in_json":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login", "terminal_reason": "api_error", "usage": {"input_tokens": 0, "output_tokens": 0}}))
        return 1
    if mode == "quota":
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Claude usage limit reached. Your limit will reset in 2 hours."}))
        return 1
    if mode == "error_json":
        print(json.dumps({"type": "result", "is_error": True, "result": "Something broke"}))
        return 1
    if mode == "bad":
        print("this is not json")
        return 0
    items = fake_common.reply_items(mode, json.loads(stdin.decode("utf-8")))
    usage = {"input_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 120,
             "output_tokens": 60}
    if mode == "result_only":
        out = {"type": "result", "is_error": False, "result": json.dumps({"items": items}), "usage": usage}
    elif mode == "schema_violation":
        out = {"type": "result", "is_error": False, "result": "", "structured_output": [], "usage": usage}
    else:
        out = {"type": "result", "is_error": False, "result": "", "structured_output": {"items": items},
               "usage": usage}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
