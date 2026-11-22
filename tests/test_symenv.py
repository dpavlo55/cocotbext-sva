"""Symbolic stepping of local-variable attempts (_symenv) must give exactly the
results of stepping concrete terms: same events (verdict, tick, start ticks,
count, so attempts merge identically) and counters, on random properties with
several locals, reassignment, conditions on values assigned in the same tick,
and several threads with different values in one attempt.

    SYM_N=3000 pytest tests/test_symenv.py
"""
import os
import random

import pytest

from cocotbext.sva import _engine, _trace
from cocotbext.sva._errors import SvaError
from cocotbext.sva._trace import run_trace

SIGNALS = {"a": 1, "b": 1, "c": 1, "d": 1, "x": 3, "y": 3}
N = int(os.environ.get("SYM_N", "150"))


class LGen:
    def __init__(self, rnd):
        self.r = rnd

    def bit(self):
        return self.r.choice(["a", "b", "c", "d", "!a", "!b", "1"])

    def val(self):
        return self.r.choice(["x", "y", "v", "w", "v + x", "w ^ y", "3'd1"])

    def cond(self):
        r = self.r
        k = r.randint(0, 5)
        if k <= 1:
            return self.bit()
        if k == 2:
            return f"{r.choice(['x', 'y'])} == {r.choice(['v', 'w'])}"
        if k == 3:
            rhs = r.choice(["x", "y", "3'd0"])         # (no nested quotes: Python 3.10)
            return f"{self.bit()} && {r.choice(['v', 'w'])} != {rhs}"
        if k == 4:
            return f"v == w || {self.bit()}"
        return f"({r.choice(['x', 'y'])} > v) || {self.bit()}"

    def item(self):
        r = self.r
        k = r.randint(0, 4)
        if k <= 1:
            return f"({self.cond()})"
        if k == 2:
            return f"({self.cond()}, {r.choice(['v', 'w'])} = {self.val()})"
        if k == 3:
            return f"({self.bit()}, v = {self.val()}, w = {self.val()})"
        return f"(({self.cond()})[*{r.randint(1, 2)}:{r.randint(2, 3)}])"

    def seq(self, depth=2):
        r = self.r
        k = r.randint(0, 5 if depth else 2)
        if k <= 2:
            out = self.item()
            for _ in range(r.randint(0, 2)):
                out += f" {r.choice(['##0', '##1', '##1', '##[0:2]', '##[1:2]'])} {self.item()}"
            return out
        if k == 3:
            return f"({self.seq(depth - 1)}) or ({self.seq(depth - 1)})"
        if k == 4:
            return f"({self.seq(depth - 1)}) and ({self.seq(depth - 1)})"
        return f"({self.seq(depth - 1)})[*1:2]"

    def head(self):
        b = self.bit
        return self.r.choice([
            f"({b()}, v = x, w = y)",
            f"({b()}, v = x, w = 3'd0) ##[0:2] ({b()}, w = y + v)",
            f"({b()}, v = x, w = y)[*1:3]",
            f"(({b()}, v = x, w = y) ##1 1)[*1:2]",
            f"({b()}, v = x, w = y) or ({b()}, v = y, w = x)",
            f"({b()}, w = y, v = x) ##1 ({b()}, v = v + w)"])

    def prop(self):
        r = self.r
        h = f"{self.head()} ##1 {self.seq(1)}" if r.random() < 0.4 else self.head()
        imp = r.choice(["|->", "|=>"])
        k = r.randint(0, 9)
        if k <= 3:
            return f"{h} {imp} {self.seq()}"
        if k == 4:
            return f"{h} {imp} not ({self.seq(1)})"
        if k == 5:
            return f"{h} {imp} ({self.seq(1)}) and ({self.seq(1)})"
        if k == 6:
            return f"{h} {imp} if ({self.cond()}) ({self.seq(1)}) else ({self.seq(1)})"
        if k == 7:
            return f"{h} {imp} nexttime ({self.seq(1)})"
        if k == 8:
            return f"{h} {imp} always [0:2] ({self.seq(0)})"
        return f"{h} {r.choice(['#-#', '#=#'])} {self.seq(1)}"


def _rows(rnd, n):
    dens = rnd.choice([0.3, 0.6, 0.85])
    return [{"a": int(rnd.random() < dens), "b": int(rnd.random() < dens),
             "c": int(rnd.random() < dens), "d": int(rnd.random() < dens),
             "x": rnd.randint(0, 3), "y": rnd.randint(0, 3)} for _ in range(n)]


def _run(props, rows, kinds, symbolic, monkeypatch, quiet):
    monkeypatch.setattr(_engine, "SYMBOLIC", symbolic)
    if quiet:
        monkeypatch.setattr(_trace._Sink, "wants_pass", False)
        monkeypatch.setattr(_trace._Sink, "wants_vacuous", False)
    r = run_trace(props, SIGNALS, rows, kinds=kinds)
    events = {}
    for e in r.events:
        events.setdefault(e.assertion, []).append((e.kind, e.tick, e.starts, e.count))
    return events, r.stats, r.warnings


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("seed", range(4))
def test_symbolic_matches_concrete(seed, quiet, monkeypatch):
    rnd = random.Random(1000 + seed)
    g = LGen(rnd)
    checked = 0
    for _ in range(N):
        props, kinds = {}, {}
        for k in range(rnd.randint(1, 3)):
            props[f"p{k}"] = g.prop()
            kinds[f"p{k}"] = rnd.choice(["assert", "assert", "cover"])
        rows = _rows(rnd, rnd.randint(5, 50))
        try:
            conc = _run(props, rows, kinds, False, monkeypatch, quiet)
        except (SvaError, TypeError):
            continue
        sym = _run(props, rows, kinds, True, monkeypatch, quiet)
        assert sym == conc, f"{props}\n{rows}"
        checked += 1
    assert checked > 0.5 * N


def test_shapes_stay_finite(monkeypatch):
    """Captured values do not enter the shapes: a long run with ever new
    values creates few shapes, and attempts with equal terms merge."""
    monkeypatch.setattr(_engine, "SYMBOLIC", True)
    rnd = random.Random(3)
    rows = [{"a": int(rnd.random() < 0.5), "b": int(rnd.random() < 0.3), "c": 0, "d": 0,
             "x": rnd.randint(0, 7), "y": rnd.randint(0, 7)} for _ in range(3000)]
    tr = _trace.TraceRun({"p": "(a, v = x) ##1 (b, w = y + v) |-> ##[1:4] y == w"}, SIGNALS)
    for r in rows:
        tr.step(r)
    mon = tr.mons[0]
    assert len(mon._sym.shapes) < 60
    tr.finish()
