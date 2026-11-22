"""Multi-clock properties (rewritten to single-clock ones over the union of
their clocks' ticks, see cocotbext.sva._multiclock).

    pytest tests/test_multiclock.py
    FUZZ_N=3000 FUZZ_SEED=5 pytest tests/test_multiclock.py -x
"""
import os
import random

import pytest

from cocotbext.sva import _engine
from cocotbext.sva._errors import SvaError
from cocotbext.sva._parser import parse_assertion
from cocotbext.sva._trace import run_trace
from test_fuzz import SIGNALS, Gen

_engine.MAX_STARTS = 1 << 30
N = int(os.environ.get("FUZZ_N", "300"))
SEED = int(os.environ.get("FUZZ_SEED", "1"))


def rows_of(rnd, n, clocks=("c0", "c1"), p_tick=0.5):
    """Random time steps; in each at least one clock ticks."""
    rows = []
    while len(rows) < n:
        r = {"a": rnd.randint(0, 1), "b": rnd.randint(0, 1), "c": rnd.randint(0, 1),
             "d": rnd.randint(0, 1), "x": rnd.randint(0, 7), "y": rnd.randint(0, 7)}
        for c in clocks:
            r[c] = int(rnd.random() < p_tick)
        if any(r[c] for c in clocks):
            rows.append(r)
    return rows


def events(result, tick_map=None):
    """One (kind, tick, start tick) per attempt (the engine may report
    several attempts with the same verdict at once)."""
    out = []
    f = (lambda t: t) if tick_map is None else tick_map.__getitem__
    for e in result.events:
        assert e.count == len(e.starts)
        for s in e.starts:
            out.append((e.kind, f(e.tick) if e.kind != "end_fail" else None, f(s)))
    return sorted(out, key=repr)


# --------------------------------------------------------------------------
# Projection: a single-clock property evaluated through the multi-clock
# rewriting, on time steps where its clock ticks or not, must give exactly
# the results of the plain engine on the rows where the clock ticks. This
# checks the rewriting of every operator against the engine's own operators.
# --------------------------------------------------------------------------

def _projection(text, rows, kind="assert", once=False):
    proj = [r for r in rows if r["c0"]]
    idx = [i for i, r in enumerate(rows) if r["c0"]]
    kw = dict(kinds={"p": kind}, once=("p",) if once else ())
    try:
        ref = run_trace({"p": text}, SIGNALS, proj, **kw)
    except SvaError:
        return None
    if ref.warnings:
        return None
    got = run_trace({"p": text}, SIGNALS, rows, clock="c0", force_multiclock=True, **kw)
    return events(ref, idx), ref.stats["p"], events(got), got.stats["p"]


def _check_projection(text, rows, kind="assert", once=False):
    res = _projection(text, rows, kind, once)
    if res is None:
        return False
    ref_ev, ref_st, got_ev, got_st = res
    assert got_ev == ref_ev, f"{text}\n rows={rows}\n engine   {ref_ev}\n rewritten {got_ev}"
    assert got_st == ref_st, f"{text}\n {ref_st}\n {got_st}"
    return True


DIRECTED = [
    "a |-> ##[1:3] b", "a |=> b ##1 c", "a ##1 b |-> c", "a |-> ##[0:2] b", "##2 a |-> b",
    "a |-> b[*2] ##1 c", "a |-> b[*1:$] ##1 c", "a[*0:2] ##1 b |-> c", "a |-> b[->2] ##1 c",
    "a |-> b[=2] ##1 c", "a |-> ##[2:$] b", "a ##[*] b |-> c", "(a ##0 b) |-> c ##0 d",
    "a |-> (b ##1 c) or (c ##2 d)", "a |-> (b ##1 c) and (c[*3])", "a |-> (b[*2]) intersect (c ##1 d)",
    "a |-> (b ##1 c) within (d[*4])", "a |-> b throughout (c ##2 d)", "a |-> first_match(b ##[1:3] c)",
    "not (a ##1 b)", "(a |-> b) and (c |-> d)", "(a |-> b) or (c |=> d)", "if (a) b ##1 c else d",
    "a #-# b ##1 c", "a #=# c", "(a ##1 b) implies c", "(a |-> b) iff c",
    "a |-> strong(##[1:$] b)", "strong(a ##[1:3] b)", "a |-> weak(b ##1 c)",
    "$rose(a) |=> b", "$fell(a) |-> c", "$stable(x) |-> !b", "a |-> x == $past(x) + 3'd1",
    "a |-> x == $past(y, 2)", "b |-> $past(a, 1, c)",
    "(a, v = x) |-> ##2 y == v", "(a, v = x) |=> (c, v = v + 3'd1) ##1 y == v",
    "a |-> nexttime b", "a |-> nexttime[2] b", "a |-> s_nexttime b", "a |-> nexttime[0] b",
    "a |-> always [1:3] b", "a |-> s_always [1:3] b", "a |-> always b", "a |-> always [2:$] b",
    "a |-> eventually [1:3] b", "a |-> s_eventually b", "a |-> s_eventually [1:3] b",
    "a |-> s_eventually [2:$] b", "a |-> b until c", "a |-> b s_until c", "a |-> b until_with c",
    "a |-> b s_until_with c", "a |-> (b ##1 c) until d", "a |-> (b |-> c) until d",
    "accept_on (c) a |-> ##[1:3] b", "reject_on (c) a |-> ##[1:3] b",
    "sync_accept_on (c) a |=> b ##1 b", "a |-> sync_reject_on (c) s_eventually b",
    "case (x) 0: a; 1, 2: b ##1 c; default: d; endcase", "a |=> if (b) c else d",
    "a |=> nexttime b", "a |=> always [0:1] b", "a |=> b until c",
]


@pytest.mark.parametrize("text", DIRECTED)
def test_projection_directed(text):
    rnd = random.Random(hash(text) & 0xFFFF)
    for _ in range(25):
        rows = rows_of(rnd, 24, p_tick=rnd.choice([0.3, 0.5, 0.8]))
        assert _check_projection(text, rows)


@pytest.mark.parametrize("seed", range(SEED, SEED + 4))
def test_projection_fuzz(seed):
    rnd = random.Random(seed)
    g = Gen(rnd)
    done = 0
    while done < N:
        text = g.prop(rnd.randint(1, 3))
        rows = rows_of(rnd, rnd.randint(4, 16), p_tick=rnd.choice([0.3, 0.6, 0.9]))
        done += bool(_check_projection(text, rows))


@pytest.mark.parametrize("seed", range(SEED + 10, SEED + 12))
def test_projection_fuzz_locals(seed):
    rnd = random.Random(seed)
    g = Gen(rnd, locals_ok=True)
    done = 0
    while done < N // 2:
        text = g.local_prop()
        rows = rows_of(rnd, rnd.randint(4, 14), p_tick=rnd.choice([0.3, 0.6, 0.9]))
        done += bool(_check_projection(text, rows))


@pytest.mark.parametrize("seed", range(SEED + 20, SEED + 24))
def test_projection_fuzz_temporal(seed):
    rnd = random.Random(seed)
    g = Gen(rnd, temporal=True)
    done = 0
    while done < N:
        text = g.prop(rnd.randint(1, 3))
        rows = rows_of(rnd, rnd.randint(4, 14), p_tick=rnd.choice([0.3, 0.6, 0.9]))
        done += bool(_check_projection(text, rows))


def test_projection_cover_and_once():
    rnd = random.Random(3)
    for text in ("a ##1 b ##1 c", "a ##[1:3] b", "a[*2] ##1 !a"):
        for _ in range(20):
            assert _check_projection(text, rows_of(rnd, 20), kind="cover")
    for text in ("always (a |-> ##[1:2] b)", "s_eventually (a && b)", "a until b", "nexttime[3] a",
                 "always [2:$] (a || b)", "s_nexttime[30] a"):
        for _ in range(20):
            assert _check_projection(text, rows_of(rnd, 20), once=True)


# --------------------------------------------------------------------------
# Clock changes
# --------------------------------------------------------------------------

def R(**kw):
    r = {"c1": 0, "c2": 0, "a": 0, "b": 0, "c": 0}
    r.update(kw)
    return r


def run1(text, rows, **kw):
    r = run_trace({"p": text}, {"a": 1, "b": 1, "c": 1}, rows, **kw)
    return events(r), r.stats["p"]


def test_concatenation_moves_to_a_strictly_later_tick():
    rows = [R(c1=1, a=1), R(c2=1, b=1),          # ordinary case
            R(c1=1, c2=1, a=1, b=1),             # both clocks: b here does not count
            R(c2=1, b=0),                        # ... this tick does: no match
            R(c1=1)]
    ev, st = run1("@(posedge c1) a ##1 @(posedge c2) b |-> 1", rows)
    assert ev == sorted([("pass", 1, 0), ("vpass", 3, 2), ("vpass", 4, 4)], key=repr)
    assert st["attempts"] == 3                   # ticks of c1 only


def test_fusion_and_overlapped_implication_may_use_the_same_tick():
    rows = [R(c1=1, c2=1, a=1, b=1),             # c2 ticks with c1: b sampled here
            R(c1=1, a=1), R(c2=1, b=0),          # else at the next tick of c2
            R(c1=1, a=1), R(c1=1), R(c2=1, b=1)]
    for text in ("@(posedge c1) a |-> @(posedge c2) b",
                 "(@(posedge c1) a ##0 @(posedge c2) b) or (@(posedge c1) !a)"):
        ev, st = run1(text, rows)
        fails = [e for e in ev if e[0] == "fail"]
        assert fails == [("fail", 2, 1)], (text, ev)
    ev, st = run1("@(posedge c1) a |-> @(posedge c2) b", rows)
    assert ("pass", 0, 0) in ev and ("pass", 5, 3) in ev


def test_nonoverlapped_implication():
    rows = [R(c1=1, c2=1, a=1, b=1), R(c2=1, b=0), R(c1=1, a=1), R(c1=1), R(c2=1, b=1)]
    ev, st = run1("@(posedge c1) a |=> @(posedge c2) b", rows)
    assert ("fail", 1, 0) in ev and ("pass", 4, 2) in ev
    assert st == {"attempts": 3, "passes": 1, "vacuous": 1, "failures": 1, "disabled": 0,
                  "pending_fail": 0}


def test_clock_flows_into_the_consequent():
    # 16.13.3: b ##1 c is clocked by c2, and so is the consequent
    rows = [R(c1=1, a=1), R(c1=1), R(c2=1, b=1), R(c1=1, c=1), R(c2=1, c=1), R(c1=1), R(c2=1, a=1)]
    ev, _ = run1("@(posedge c1) a ##1 @(posedge c2) b ##1 c |=> a", rows)
    assert ("pass", 6, 0) in ev


def test_default_clock_and_parentheses():
    rows = [R(c1=1, a=1), R(c2=1, b=1), R(c1=1, c=1), R(c2=1, c=0), R(c1=1)]
    # the clocking event does not extend out of the parentheses: c is on c1
    ev, _ = run1("a ##1 (@(posedge c2) b) ##1 c", rows, clock="c1", kinds={"p": "cover"})
    assert ev == [("pass", 2, 0)]
    # without them, c is on c2
    ev, _ = run1("a ##1 @(posedge c2) b ##1 c", rows, clock="c1", kinds={"p": "cover"})
    assert ev == []


def test_sampled_value_functions_follow_their_clock():
    rows = [R(c1=1, a=0), R(c2=1, a=1), R(c1=1, a=1), R(c2=1, a=1), R(c1=1, a=1),
            R(c1=1, c2=1, a=0), R(c2=1, a=1)]
    # $rose on c1 compares with the previous tick of c1, not the previous row
    ev, _ = run1("@(posedge c1) $rose(a) |=> @(posedge c2) 1", rows)
    assert [e for e in ev if e[0] == "pass"] == [("pass", 3, 2)]
    # explicit clocking event
    ev, _ = run1("@(posedge c1) a |-> $past(a, 1, 1, @(posedge c2)) ##1 @(posedge c2) 1", rows)
    assert [e[:2] for e in ev if e[0] == "fail"] == []


def test_same_clock_everywhere_is_single_clock():
    rows = [R(c1=1, a=1), R(c1=1, b=1), R(c1=1)]
    ev, st = run1("@(posedge c1) a |=> @(posedge c1) b", rows)
    assert ev == sorted([("pass", 1, 0), ("vpass", 1, 1), ("vpass", 2, 2)], key=repr)


@pytest.mark.parametrize("text, msg", [
    ("@(posedge c1) a ##2 @(posedge c2) b", "only ##1 and ##0"),
    ("@(posedge c1) a ##[1:2] @(posedge c2) b", "only ##1 and ##0"),
    ("(@(posedge c1) a) and (@(posedge c2) b)", "single leading clock"),
    ("@(posedge c1) a |-> (b ##1 @(posedge c2) c) intersect (b ##1 c)", "must end on the same clock"),
    ("@(posedge c1) (a ##1 @(posedge c2) b)[*2]", "must end on the clock"),
])
def test_errors(text, msg):
    with pytest.raises(SvaError, match=msg):
        run_trace({"p": text}, {"a": 1, "b": 1, "c": 1}, [])


def test_parser_scopes():
    from cocotbext.sva import _ast as A
    s = parse_assertion("@(posedge c1) a ##1 @(posedge c2) b ##1 c |=> @(negedge c1) d")
    assert s.clock.edge == "posedge"
    impl = s.prop
    assert isinstance(impl, A.PImpl) and isinstance(impl.prop.seq, A.Clocked)
    assert impl.prop.seq.clock.edge == "negedge"
    assert isinstance(impl.seq.right, A.Clocked) and isinstance(impl.seq.right.body, A.SDelay)
