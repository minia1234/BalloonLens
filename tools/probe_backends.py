"""Checks the subscription CLIs on this PC.

Without --live it reads the CLIs' own status (version, login) and runs the Codex sandbox check. That check uses
the Windows sandbox runner on two marker files and calls no model. With --live it makes at most these model
calls, each on three short fixed lines:
  - Claude Code: 1 call (Haiku, low effort)
  - Codex: 1 translation call, only if the sandbox check passed
Nothing about the account is printed or saved: only versions, state names, counts, token totals and the
names of Codex item types (for example "agent_message"). Results go to bench_out/probes/ (not shipped).

Usage:
  python tools/probe_backends.py                 # read-only status
  python tools/probe_backends.py --live          # spends at most 3 calls of subscription usage
  python tools/probe_backends.py --live --record # also stores a passed sandbox check for the helper
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helper"))

from balloonlens import config  # noqa: E402
from balloonlens.backends.base import BackendError  # noqa: E402
from balloonlens.backends.claude_cli import ClaudeCodeBackend  # noqa: E402
from balloonlens.backends.codex_cli import CodexBackend  # noqa: E402
from balloonlens.backends.prompts import PAGE_SCHEMA_JSON, build_payload, encode_payload, system_prompt  # noqa: E402
from balloonlens.cache import Cache  # noqa: E402

# Harmless, fixed sample lines: the same text on every run, so results can be compared.
SAMPLE = [(1, "待って！"), (2, "もう行くよ。"), (3, "さようなら。")]
SAMPLE_IDS = [sid for sid, _ in SAMPLE]


def translate_once(backend, model, schema_json, schema_name, **extra):
    payload = encode_payload(build_payload("ja", "ko", SAMPLE, SAMPLE_IDS))
    started = time.time()
    try:
        res = backend.complete_json(system_prompt("ja", "ko"), payload, model=model, schema_json=schema_json,
                                    schema_name=schema_name, **extra)
    except BackendError as e:
        return {"ok": False, "code": e.code, "detail": e.detail[:120], "seconds": round(time.time() - started, 2)}
    items = res["data"].get("items", []) if isinstance(res["data"], dict) else []
    return {"ok": True, "items": len(items), "ids_match": sorted(i.get("id") for i in items) == SAMPLE_IDS,
            "usage": res["usage"], "seconds": res["seconds"]}


def probe_claude(args):
    backend = ClaudeCodeBackend(command=config.claude_command(), config_dir=Path(args.claude_config_dir),
                                sandbox_dir=config.CLAUDE_SANDBOX_DIR, runtime_dir=config.RUNTIME_DIR,
                                model=config.CLAUDE_MODEL, effort=config.CLAUDE_EFFORT, timeout=config.CLI_TIMEOUT_S)
    status = backend.preflight()
    report = {"state": status.state, "version": status.version, "detail": status.detail,
              "effort_flag": config.CLAUDE_EFFORT}
    if args.live:
        if status.state == "ready":
            report["call"] = translate_once(backend, config.CLAUDE_MODEL, PAGE_SCHEMA_JSON, "page")
        else:
            report["call"] = {"skipped": status.state}
    return report


def probe_codex(args, cache):
    backend = CodexBackend(command=config.codex_command(), sandbox_dir=config.CODEX_SANDBOX_DIR,
                           runtime_dir=config.RUNTIME_DIR, model=config.CODEX_MODEL,
                           reasoning=config.CODEX_REASONING, timeout=config.CLI_TIMEOUT_S, sandbox_verified=False)
    status = backend.preflight()
    report = {"state": status.state, "detail": status.detail, "reasoning_flag": config.CODEX_REASONING}
    if status.state in ("not_installed", "not_logged_in", "backend_error", "api_key_detected", "unsupported"):
        return report
    # Same Windows sandbox runner as `codex exec -s read-only`, on two marker files. No model call, no usage.
    working = Path(args.sandbox_dir) if args.sandbox_dir else config.CODEX_SANDBOX_DIR
    check = backend.local_sandbox_check(working, config.RUNTIME_DIR / "canary-outside-sandbox")
    check["working_dir"] = str(working)
    report["sandbox"] = check
    if args.record:
        cache.put_flags("codex", {**check, "checked_at": time.time(), "source": "probe_backends.py"})
    backend.set_sandbox_result(check["verdict"])
    report["state_after_check"] = backend.preflight().state
    if not args.live:
        return report
    if not check["passed"]:
        report["call"] = {"skipped": check["verdict"]}
        return report
    report["call"] = translate_once(backend, config.CODEX_MODEL, PAGE_SCHEMA_JSON, "page")
    report["item_types"] = backend.last_item_types
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="make the small model calls (uses subscription usage)")
    parser.add_argument("--record", action="store_true", help="store a passed Codex sandbox check in the helper data")
    parser.add_argument("--backend", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--claude-config-dir", default=str(config.CLAUDE_CONFIG_DIR))
    parser.add_argument("--out", default="", help="result file (default: bench_out/probes/<time>.json)")
    parser.add_argument("--sandbox-dir", default="",
                        help="working folder for the Codex sandbox check (default: the app's folder). The sandbox "
                             "cannot start inside the user profile on the test PC, so the check can be pointed "
                             "at another folder to see what the sandbox can read")
    args = parser.parse_args()
    if args.live:
        print("Live mode: Claude Code 1 call at most; Codex 1 canary + 1 call at most.")
    result = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "helper_version": config.VERSION,
              "live": args.live, "backends": {}}
    cache = Cache(config.DB_PATH) if args.record else None
    if args.backend in ("claude", "both"):
        result["backends"]["claude"] = probe_claude(args)
    if args.backend in ("codex", "both"):
        result["backends"]["codex"] = probe_codex(args, cache)
    out = Path(args.out) if args.out else ROOT / "bench_out" / "probes" / f"probe-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
