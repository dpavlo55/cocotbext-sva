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


def _run_mc(case, kernel, quiet, monkeypatch):
    from test_corpus_mc import DATA
    clocks = DATA["clocks"]
    rows = [row for row in case["trace"] if any(row[c] for c in clocks)]
    saved = _engine._kmod
    if not kernel:
        _engine._kmod = None
    try:
        monkeypatch.setattr(_trace._Sink, "wants_pass", not quiet)
        monkeypatch.setattr(_trace._Sink, "wants_vacuous", not quiet)
        tr = _trace.TraceRun(case["props"], DATA["signals"], kinds=case["kinds"])
        for r in rows:
            tr.step(r)
        res = tr.finish()
    finally:
        _engine._kmod = saved
    in_kernel = sum(m._k is not None for m in tr.mons)
    events = {}
    for e in res.events:
        events.setdefault(e.assertion, []).append((e.kind, e.tick, e.starts, e.count))
    return (events, res.stats), in_kernel


@pytest.mark.parametrize("quiet", [False, True])
def test_kernel_matches_python_multiclock(quiet, monkeypatch):
    """Multi-clock assertions (attempts gated on the leading clock) give the
    same events and counters in the kernel as in Python."""
    from test_corpus_mc import DATA
    checked = in_kernel = 0
    for case in DATA["cases"]:
        try:
            py, _ = _run_mc(case, False, quiet, monkeypatch)
        except SvaError:
            continue
        native, k = _run_mc(case, True, quiet, monkeypatch)
        assert native == py, case["id"]
        checked += 1
        in_kernel += k
    assert checked > 300 and in_kernel > 300


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("seed", range(3))
def test_kernel_matches_python_local_variables(seed, quiet, monkeypatch):
    """Local-variable assertions (symbolic stepping, attempts kept in the
    kernel) give the same events, merge counts and counters in the kernel as
    in Python, on random properties with several locals, reassignment,
    conditions on locals and several threads per attempt."""
    from test_symenv import SIGNALS as LSIGNALS
    from test_symenv import LGen, _rows
    monkeypatch.setattr(_engine, "SYMBOLIC", True)
    monkeypatch.setattr(_trace._Sink, "wants_pass", not quiet)
    monkeypatch.setattr(_trace._Sink, "wants_vacuous", not quiet)
    rnd = random.Random(2000 + seed)
    g = LGen(rnd)
    checked = in_kernel = 0
    for _ in range(120):
        props, kinds = {}, {}
        for k in range(rnd.randint(1, 3)):
            props[f"p{k}"] = g.prop()
            kinds[f"p{k}"] = rnd.choice(["assert", "assert", "cover"])
        rows = _rows(rnd, rnd.randint(5, 50))
        out = []
        for kernel in (False, True):
            saved = _engine._kmod
            if not kernel:
                _engine._kmod = None
            try:
                tr = _trace.TraceRun(props, LSIGNALS, kinds=kinds)
                for r in rows:
                    tr.step(r)
                res = tr.finish()
            except (SvaError, TypeError):
                break
            finally:
                _engine._kmod = saved
            events = {}
            for e in res.events:
                events.setdefault(e.assertion, []).append((e.kind, e.tick, e.starts, e.count))
            out.append((events, res.stats))
            if kernel:
                in_kernel += sum(m._k is not None for m in tr.mons)
        if len(out) < 2:
            continue
        assert out[1] == out[0], f"{props}\n{rows}"
        checked += 1
    assert checked > 60 and in_kernel > 60
