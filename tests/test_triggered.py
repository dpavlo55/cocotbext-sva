"""`seq.triggered` (and its old name `seq.ended`): true at the ticks where a
match of the named sequence ends, whatever tick it started at.

    pytest tests/test_triggered.py
    FUZZ_N=3000 FUZZ_SEED=5 pytest tests/test_triggered.py
"""
import os
import random

import pytest

from cocotbext.sva import _engine
from cocotbext.sva._errors import SvaError, SvaUnsupportedError
from cocotbext.sva._trace import run_trace
from refsem import Ref, Word
from test_fuzz import SIGNALS, Gen, random_trace

_engine.MAX_STARTS = 1 << 30
N = int(os.environ.get("FUZZ_N", "400"))
SEED = int(os.environ.get("FUZZ_SEED", "1"))


def triggered(seq, rows, **kw):
    """Ticks at which seq.triggered is true (as the engine sees it)."""
    r = run_trace({"p": "s.triggered"}, SIGNALS, rows, named={"s": seq}, **kw)
    return sorted(e.tick for e in r.events if e.kind == "pass")


def end_points(seq, rows):
    """The same from the reference interpreter: ends of the nonempty matches
    of seq from every start tick."""
    ref = Ref(seq, rows)
    w = Word(rows, len(rows), None, 0)
    ends = set()
    for i in range(len(rows)):
        ends |= {e for e, _ in ref.M(ref.spec.prop, i, (), w) if e >= i}
    return sorted(ends)


def test_directed():
    rows = [dict(a=a, b=b, c=0, d=0, x=0, y=0) for a, b in
            [(1, 0), (0, 1), (0, 1), (1, 1), (1, 0), (0, 0), (0, 1), (1, 1), (1, 1), (0, 1)]]
    assert triggered("a ##1 b", rows) == [1, 8, 9]
    assert triggered("a ##[1:2] b", rows) == [1, 2, 6, 8, 9]
    assert triggered("a[*2]", rows) == [4, 8]
    assert triggered("a", rows) == [0, 3, 4, 7, 8]
    # every match counts, not only the first one of an attempt: the attempt
    # of tick 0 ends at 1 and at 2
    assert triggered("a ##[1:$] b", rows)[:2] == [1, 2]
    assert triggered("first_match(a ##[1:$] b)", rows) == [1, 6, 8, 9]


@pytest.mark.parametrize("seed", range(SEED, SEED + 4))
def test_end_points_match_the_reference(seed):
    rnd = random.Random(seed)
    g = Gen(rnd)
    done = 0
    while done < N:
        seq = g.seq(rnd.randint(1, 3))
        rows = random_trace(rnd, rnd.randint(3, 12))
        try:
            got = triggered(seq, rows)
        except SvaError:
            continue
        assert got == end_points(seq, rows), f"{seq}\n{rows}"
        done += 1


def test_used_in_properties():
    rows = [dict(a=a, b=b, c=c, d=0, x=0, y=0) for a, b, c in
            [(1, 0, 0), (0, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 0), (1, 0, 1), (0, 1, 1)]]
    named = {"s": "a ##1 b"}
    r = run_trace({"p": "s.triggered |-> c", "q": "c |-> s.ended", "r": "$rose(s.triggered) |=> !s.triggered",
                   "cov": "s.triggered ##2 s.triggered"},
                  SIGNALS, rows, named=named, kinds={"cov": "cover"})
    assert r.fails("p") == [(3, (3,), 1)]
    assert r.fails("q") == [(5, (5,), 1)]
    assert r.fails("r") == []
    assert sorted(e.tick for e in r.events if e.assertion == "cov" and e.kind == "pass") == [3]
    # equivalent to the same condition written with $past
    r2 = run_trace({"p": "(b && $past(a)) |-> c"}, SIGNALS, rows, initial={k: 0 for k in SIGNALS})
    assert r2.fails("p") == r.fails("p")


def test_nested_and_shared():
    rows = [dict(a=a, b=b, c=0, d=0, x=0, y=0) for a, b in [(1, 0), (1, 1), (0, 1), (1, 1), (0, 0)]]
    named = {"s": "a ##1 b", "t": "s.triggered ##1 a"}
    r = run_trace({"p": "t.triggered", "q": "s.triggered && t.triggered"}, SIGNALS, rows, named=named)
    # s ends at 1, 2; t = (s ends) ##1 a ends at 3
    assert sorted(e.tick for e in r.events if e.assertion == "p" and e.kind == "pass") == [3]
    assert [e.tick for e in r.events if e.assertion == "q" and e.kind == "pass"] == []


def test_across_clocks():
    """s is clocked by c2; the property by c1. s.triggered is true in the
    time steps where a match of s ends (at a tick of c2)."""
    def R(c1, c2, a=0, b=0, c=0):
        return dict(c1=c1, c2=c2, a=a, b=b, c=c)
    rows = [R(0, 1, a=1), R(1, 0, c=1), R(1, 1, b=1, c=1), R(1, 0, c=1), R(0, 1, a=1), R(1, 1, b=1, c=1),
            R(0, 1, b=1), R(1, 0, c=1)]
    named = {"s": "@(posedge c2) a ##1 b"}
    sig = {"a": 1, "b": 1, "c": 1}
    # s ends in steps 2 (a at step 0, b at the next c2 tick) and 5
    r = run_trace({"p": "@(posedge c1) c |-> s.triggered"}, sig, rows, named=named)
    assert [(t, s) for t, s, _ in r.fails("p")] == [(1, (1,)), (3, (3,)), (7, (7,))]
    assert sorted(e.tick for e in r.events if e.kind == "pass") == [2, 5]
    assert r.stats["p"]["attempts"] == 5                 # ticks of c1
    # wait for the end of a sequence on another clock
    r = run_trace({"p": "@(posedge c1) c ##1 s.triggered[->1] ##1 c"}, sig, rows, named=named,
                  kinds={"p": "cover"})
    assert sorted((e.tick, e.starts) for e in r.events if e.kind == "pass") == [(3, (1,)), (7, (2, 3))]


@pytest.mark.parametrize("text, named, exc, msg", [
    ("s.triggered", {"s": "(a, v = x) ##1 y == v"}, SvaUnsupportedError, "local variables"),
    ("s.triggered", {"s": "a ##1 s.triggered"}, SvaError, "inside sequence 's' itself"),
])
def test_errors(text, named, exc, msg):
    with pytest.raises(exc, match=msg):
        run_trace({"p": text}, SIGNALS, [], named=named)


def test_sequence_with_arguments_is_rejected():
    from cocotbext.sva._compile import NamedDef
    from cocotbext.sva._parser import parse_sequence_or_property
    named = {"s": NamedDef("s", parse_sequence_or_property("p ##1 q"), ("p", "q"), "p ##1 q")}
    with pytest.raises(SvaUnsupportedError, match="arguments"):
        run_trace({"p": "s.triggered"}, SIGNALS, [], named=named)
