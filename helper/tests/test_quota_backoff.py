"""Repeated limit hits wait longer: 30 minutes, then 60, then 120, then stay at 120 (plan §9.4). A success starts over."""
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.cache import Cache


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def limit_hit(policy):
    policy.record(backend="claude", model="claude-haiku-5-5", lines=1, usage=None, seconds=0.1,
                  outcome="quota_exhausted")


def ok_call(policy):
    policy.record(backend="claude", model="claude-haiku-5-5", lines=1, usage={"input": 10, "output": 5},
                  seconds=0.1, outcome="ok")


def test_repeated_limit_hits_double_the_wait_up_to_two_hours(tmp_path):
    clock = Clock()
    cache = Cache(tmp_path / "c.db")
    policy = QuotaPolicy(cache, clock=clock)
    waits = []
    for _ in range(4):
        limit_hit(policy)
        waits.append(round(cache.get_pause("claude")["until"] - clock.now))
        policy.resume("claude")
    assert waits == [1800, 3600, 7200, 7200]


def test_a_successful_call_starts_the_wait_again(tmp_path):
    clock = Clock()
    cache = Cache(tmp_path / "c.db")
    policy = QuotaPolicy(cache, clock=clock)
    limit_hit(policy)
    limit_hit(policy)
    policy.resume("claude")
    ok_call(policy)
    limit_hit(policy)
    assert round(cache.get_pause("claude")["until"] - clock.now) == 1800
