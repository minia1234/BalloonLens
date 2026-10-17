"""Claude CLI processes in stream-json mode, some of them started ahead of time.

Measured with Haiku on this PC (a short page, MCP connectors off): a one-shot `claude -p` call takes about 3.1 s. The
same call on a process that was just started in stream-json mode takes about 1.9 s, and on one that was already
waiting about 1.1 s. So the pool keeps a few started processes for the next calls.

Each process answers exactly one request and is then closed. No page ever reaches a process that has seen another
page, and nothing is kept between calls.
"""
import atexit
import json
import queue
import subprocess
import threading
import time

from .base import _NO_WINDOW, CANCEL_POLL_S, BackendError, CallCancelled, classify_failure, kill_tree

READY_AFTER_S = 8.0  # a CLI that never answers the start-up request counts as ready after this long
MAX_KEYS = 2  # a translation key and, when review is on, a proofreading key
STDERR_LIMIT = 64 * 1024
JANITOR_EVERY_S = 30.0
# Sent as soon as a process starts. The CLI answers it once it reads input (about 0.6 s with MCP connectors off),
# which tells the pool the process has started. It is a control message, so it makes no model call.
INITIALIZE = json.dumps({"type": "control_request", "request_id": "balloonlens-init",
                         "request": {"subtype": "initialize"}}) + "\n"


class WarmProcessGone(Exception):
    """The process was gone before the request reached it, so nothing was sent and another process may be used."""


class WarmProcess:
    def __init__(self, argv, cwd, env, popen=subprocess.Popen):
        self.born = time.monotonic()
        self.proc = popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=str(cwd), env=dict(env), shell=False, **_NO_WINDOW)
        self._started = threading.Event()  # set by the CLI's first message: it has finished starting
        self._events = queue.Queue()
        self._stderr = bytearray()
        threading.Thread(target=self._read_stdout, name="cli-warm-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="cli-warm-err", daemon=True).start()
        try:
            self.proc.stdin.write(INITIALIZE.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass  # the process is already gone; ask() says so

    def _read_stdout(self):
        for raw in self.proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._started.set()
                self._events.put(event)
        self._events.put(None)  # the process ended

    def _read_stderr(self):
        for raw in self.proc.stderr:
            if len(self._stderr) < STDERR_LIMIT:
                self._stderr += raw

    def alive(self) -> bool:
        return self.proc.poll() is None

    def age(self) -> float:
        return time.monotonic() - self.born

    def is_ready(self) -> bool:
        return self.alive() and (self._started.is_set() or self.age() > READY_AFTER_S)

    def stderr_text(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace")

    def ask(self, text: str, *, timeout: float, should_cancel=None) -> dict:
        """Sends one user message and returns the CLI's `result` event."""
        if not self.alive():
            raise WarmProcessGone()
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise WarmProcessGone() from None
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.kill()
                raise BackendError("timeout", f"{timeout:.0f}s")
            if should_cancel is not None and should_cancel():
                self.kill()
                raise CallCancelled()
            try:
                event = self._events.get(timeout=min(CANCEL_POLL_S, left))
            except queue.Empty:
                continue
            if event is None:  # ended without a reply
                err = self.stderr_text()
                raise classify_failure(err) or BackendError("process_failed", err or "the CLI ended without a reply")
            if event.get("type") == "result":
                return event

    def kill(self):
        """Stops a process that is answering a request. The caller does not wait: taskkill takes a second or two."""
        threading.Thread(target=kill_tree, args=(self.proc,), name="cli-warm-kill", daemon=True).start()

    def discard(self):
        """Stops a process that is only waiting. It has started nothing, so a plain kill is enough and instant."""
        try:
            self.proc.kill()
        except OSError:
            pass

    def close(self):
        """Lets the process end by itself (it exits when its input closes); stops it if it does not."""
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass

        def finish():
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                kill_tree(self.proc)

        threading.Thread(target=finish, name="cli-warm-close", daemon=True).start()


class WarmPool:
    """Keeps `size` started processes for each of the most recently used keys (a key = model + system prompt).

    Idle processes use memory (about 200 MB each), so one that is older than `ttl` seconds is stopped and only
    replaced when the next call needs one."""

    def __init__(self, size: int, ttl: float, spawn):
        self.size = size
        self.ttl = ttl
        self._spawn = spawn  # spawn(key) -> WarmProcess
        self._lock = threading.Lock()
        self._by_key = {}  # key -> [WarmProcess]; the most recently used key is last
        self._janitor = None
        atexit.register(self.shutdown)

    def _touch(self, key):
        self._by_key[key] = self._by_key.pop(key, [])
        while len(self._by_key) > MAX_KEYS:
            for proc in self._by_key.pop(next(iter(self._by_key))):
                proc.discard()

    def _prune(self):
        for key, procs in self._by_key.items():
            keep = []
            for proc in procs:
                if proc.alive() and proc.age() <= self.ttl:
                    keep.append(proc)
                else:
                    proc.discard()
            procs[:] = keep

    def take(self, key, *, include_starting=False):
        """Prefer a ready process; a live call may also borrow one that is still starting."""
        with self._lock:
            self._touch(key)
            self._prune()
            procs = self._by_key[key]
            for proc in procs:
                if proc.is_ready():
                    procs.remove(proc)
                    return proc
            if include_starting and procs:
                return procs.pop(0)
        return None

    def refill(self, key):
        """Starts processes until `size` are waiting for key."""
        with self._lock:
            self._touch(key)
            self._prune()
            procs = self._by_key[key]
            while len(procs) < self.size:
                try:
                    procs.append(self._spawn(key))
                except OSError:  # the CLI cannot start; the normal call path will report why
                    break
            if procs and self._janitor is None:
                self._janitor = threading.Thread(target=self._sweep, name="cli-warm-janitor", daemon=True)
                self._janitor.start()

    def _sweep(self):
        while True:
            time.sleep(JANITOR_EVERY_S)
            with self._lock:
                self._prune()
                if not any(self._by_key.values()):
                    self._janitor = None
                    return

    def count(self, key) -> int:
        with self._lock:
            return len(self._by_key.get(key, []))

    def shutdown(self):
        with self._lock:
            for procs in self._by_key.values():
                for proc in procs:
                    proc.discard()
            self._by_key.clear()
