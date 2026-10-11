"""Test double for the claude CLI (helper/tests only). Behaviour comes from --mode, see fake_common."""
import json
import os
import sys
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


def main() -> int:
    mode, log, argv = fake_common.take_options(sys.argv[1:])
    if argv[:2] == ["auth", "status"]:
        return auth_status(mode)
    if argv == ["--version"]:
        print("2.0.1 (Claude Code)" if mode == "old_version" else "2.1.285 (Claude Code)")
        return 0
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
