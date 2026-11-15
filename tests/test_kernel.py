"""The compiled kernel (cocotbext-sva-kernel) must give exactly the results of
the pure-Python engine: same events (verdict, tick, start ticks, count) per
assertion, same counters. Skipped when the kernel is not installed."""
import random

import pytest

from cocotbext.sva import _engine, _trace
from cocotbext.sva._errors import SvaError
from cocotbext.sva._trace import run_trace
from test_fuzz import SIGNALS, Gen, random_trace

pytestmark = pytest.mark.skipif(_engine._kmod is None, reason="kernel not installed")


def _run(props, rows, kinds, kernel, quiet_sink=False, monkeypatch=None):
    saved = _engine._kmod
    if not kernel:
        _engine._kmod = None
    try:
        if quiet_sink:
            # a sink that wants only failures: passes and vacuous passes are
            # counted without being reported (the kernel's counter-only path)
            monkeypatch.setattr(_trace._Sink, "wants_pass", False)
            monkeypatch.setattr(_trace._Sink, "wants_vacuous", False)
        r = run_trace(props, SIGNALS, rows, kinds=kinds)
    finally:
        _engine._kmod = saved
    events = {}
    for e in r.events:
        events.setdefault(e.assertion, []).append((e.kind, e.tick, e.starts, e.count))
    return events, r.stats


def _groups(seed, n):
    rnd = random.Random(seed)
    g = Gen(rnd, temporal=bool(seed % 2))
    for _ in range(n):
        props, kinds = {}, {}
        for k in range(rnd.randint(1, 6)):
            props[f"p{k}"] = g.prop(rnd.randint(1, 3))
            kinds[f"p{k}"] = rnd.choice(["assert", "assert", "cover"])
        yield props, kinds, random_trace(rnd, rnd.randint(4, 40))


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("seed", range(4))
def test_kernel_matches_python(seed, quiet, monkeypatch):
    checked = 0
    for props, kinds, rows in _groups(seed, 60):
        try:
            py = _run(props, rows, kinds, False, quiet, monkeypatch)
        except SvaError:
            continue
        native = _run(props, rows, kinds, True, quiet, monkeypatch)
        assert native == py, f"{props}\n{rows}"
        checked += 1
    assert checked > 30


def test_start_times_survive_pruning(monkeypatch):
    """Long run with attempts always in flight: start times are pruned while
    the domain runs; every reported start time must still be right (the trace
    runner's clock: time = tick)."""
    seen = []

    def sink(self, verdict, mon, count, starts, tickno, at_end=False):
        seen.extend(starts)

    monkeypatch.setattr(_trace._Sink, "__call__", sink)
    rnd = random.Random(5)
    rows = [{"a": int(rnd.random() < 0.3), "b": int(rnd.random() < 0.3), "c": 0, "d": 0, "x": 0, "y": 0}
            for _ in range(12000)]
    run_trace({"p": "a |-> ##[1:6] b", "q": "a |=> ##[0:20] b"}, SIGNALS, rows)
    assert len(seen) > 5000
    assert all(tick == time for tick, time in seen)
