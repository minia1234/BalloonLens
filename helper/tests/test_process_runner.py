"""ProcessRunner keeps every running child, so a cancel reaches all parallel CLI calls (fake processes only)."""
import threading
import time

from balloonlens.backends import base as base_module
from balloonlens.backends.base import ProcessRunner


class FakeChild:
    def __init__(self, finish_at_once=False):
        self.returncode = None
        self.killed = False
        self._done = threading.Event()
        if finish_at_once:
            self._done.set()
            self.returncode = 0

    def communicate(self, input=None, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("child should have been cancelled")
        return b"", b""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def wait_until(predicate, timeout=2.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def start_runs(runner, count):
    threads = [threading.Thread(target=runner.run, args=(["fake-cli"],),
                                kwargs={"stdin": b"", "cwd": ".", "env": {}, "timeout": 3}, daemon=True)
               for _ in range(count)]
    for thread in threads:
        thread.start()
    return threads


def test_cancel_reaches_every_child_that_is_running(monkeypatch):
    children = []
    monkeypatch.setattr(base_module, "kill_tree", lambda child: child.kill())
    runner = ProcessRunner(popen=lambda *a, **k: children.append(FakeChild()) or children[-1])

    threads = start_runs(runner, 3)
    wait_until(lambda: len(children) == 3 and len(runner._active) == 3)
    runner.cancel()
    for thread in threads:
        thread.join(2)

    assert all(child.killed for child in children)
    assert not runner._active


def test_a_finished_child_does_not_hide_the_others(monkeypatch):
    children = []
    monkeypatch.setattr(base_module, "kill_tree", lambda child: child.kill())
    # The first child finishes at once; the second one runs until cancelled.
    script = iter([FakeChild(finish_at_once=True), FakeChild()])
    runner = ProcessRunner(popen=lambda *a, **k: children.append(next(script)) or children[-1])

    threads = start_runs(runner, 2)
    # children[0] is the quick one: the iterator hands it out on the first popen call.
    wait_until(lambda: len(children) == 2 and set(runner._active) == {children[1]})
    runner.cancel()
    for thread in threads:
        thread.join(2)

    assert children[1].killed
    assert not children[0].killed
