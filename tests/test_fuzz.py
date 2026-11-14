"""Differential fuzzing: engine vs. reference interpreter on random properties.

    pytest tests/test_fuzz.py                 # fixed seeds
    FUZZ_N=5000 FUZZ_SEED=7 pytest tests/test_fuzz.py -x
"""
import os
import random
from collections import Counter

import pytest

from cocotbext.sva import _engine
from cocotbext.sva._errors import SvaError
from cocotbext.sva._trace import run_trace
from refsem import FAIL, PASS, PASS_ANY, VPASS, Ref

# per-attempt comparison needs every start time of merged reports
_engine.MAX_STARTS = 1 << 30

SIGNALS = {"a": 1, "b": 1, "c": 1, "d": 1, "x": 3, "y": 3}


class Gen:
    def __init__(self, rnd, locals_ok=False, temporal=False):
        self.r = rnd
        self.locals_ok = locals_ok
        self.temporal = temporal       # phase 2 property operators

    def leaf(self):
        r = self.r
        return r.choice(["a", "b", "c", "d", "!a", "!b", "!c", "a && b", "c || !d", "!(a && c)",
                         "1", "a", "b"])

    def rng(self):
        r = self.r
        lo = r.randint(0, 2)
        k = r.random()
        if k < 0.15:
            return lo, None
        return lo, lo + r.randint(0, 2)

    def seq(self, depth):
        r = self.r
        if depth <= 0 or r.random() < 0.3:
            return self.leaf()
        k = r.randint(0, 13)
        s1 = lambda: self.seq(depth - 1)
        if k == 0:
            return f"{s1()} ##{r.randint(0, 2)} {s1()}"
        if k == 1:
            lo, hi = self.rng()
            return f"{s1()} ##[{lo}:{'$' if hi is None else hi}] {s1()}"
        if k == 2:
            return f"(##{r.randint(1, 2)} {s1()})"
        if k == 3:
            lo, hi = self.rng()
            return f"({s1()})[*{lo}:{'$' if hi is None else hi}]"
        if k == 4:
            return f"({s1()})[*{r.randint(1, 3)}]"
        if k == 5:
            lo, hi = self.rng()
            lo = max(lo, 1)
            hi = None if hi is None else max(hi, lo)
            return f"{self.leaf_paren()}[->{lo}:{'$' if hi is None else hi}]"
        if k == 6:
            lo, hi = self.rng()
            return f"{self.leaf_paren()}[={max(lo, 0)}:{'$' if hi is None else max(hi, lo)}]"
        if k == 7:
            return f"({s1()}) and ({s1()})"
        if k == 8:
            return f"({s1()}) or ({s1()})"
        if k == 9:
            return f"({s1()}) intersect ({s1()})"
        if k == 10:
            return f"({s1()}) within ({s1()})"
        if k == 11:
            return f"({self.leaf_paren()} throughout ({s1()}))"
        if k == 12:
            return f"first_match({s1()})"
        return f"({s1()}) ##1 ({s1()})"

    def leaf_paren(self):
        l = self.leaf()
        return f"({l})" if " " in l or l.startswith("!") else l

    def prop(self, depth):
        r = self.r
        if depth <= 0 or r.random() < 0.25:
            return f"({self.seq(2)})"
        if self.temporal and r.random() < 0.45:
            return self.temporal_prop(depth)
        k = r.randint(0, 10)
        p1 = lambda: self.prop(depth - 1)
        if k in (0, 1):
            return f"({self.seq(2)}) |-> {p1()}"
        if k in (2, 3):
            return f"({self.seq(2)}) |=> {p1()}"
        if k == 4:
            return f"not ({p1()})"
        if k == 5:
            return f"({p1()}) and ({p1()})"
        if k == 6:
            return f"({p1()}) or ({p1()})"
        if k == 7:
            return f"if ({self.leaf()}) ({p1()}) else ({p1()})"
        if k == 8:
            return f"({self.seq(2)}) #-# ({p1()})"
        if k == 9:
            return f"strong({self.seq(2)})"
        return f"if ({self.leaf()}) ({p1()})"

    def temporal_prop(self, depth):
        r = self.r
        p1 = lambda: self.prop(depth - 1)
        k = r.randint(0, 9)
        lo = r.randint(0, 2)
        hi = lo + r.randint(0, 2)
        if k == 0:
            op = r.choice(["nexttime", "s_nexttime"])
            n = r.randint(0, 2)
            return f"{op}[{n}] ({p1()})" if r.random() < 0.7 else f"{op} ({p1()})"
        if k == 1:
            return f"always [{lo}:{hi}] ({p1()})"
        if k == 2:
            return f"s_always [{lo}:{hi}] ({p1()})"
        if k == 3:
            return f"always [{lo}:$] ({p1()})" if r.random() < 0.5 else f"always ({p1()})"
        if k == 4:
            return f"eventually [{lo}:{hi}] ({p1()})"
        if k == 5:
            return r.choice([f"s_eventually [{lo}:{hi}] ({p1()})", f"s_eventually [{lo}:$] ({p1()})",
                             f"s_eventually ({p1()})"])
        if k in (6, 7):
            op = r.choice(["until", "s_until", "until_with", "s_until_with"])
            return f"({p1()}) {op} ({p1()})"
        op = r.choice(["accept_on", "reject_on", "sync_accept_on", "sync_reject_on"])
        return f"{op} ({self.leaf()}) ({p1()})"

    def local_prop(self):
        r = self.r
        n = r.randint(1, 3)
        ante = f"({self.seq(1)}, v = x)"
        cons = r.choice([f"##{n} (y == v)", f"##[1:{n}] (y == v)", f"(y != v)[*{n}]",
                         f"##1 ((y == v) ##1 b)", f"(b && y == v)[->1]"])
        return f"{ante} |-> {cons}"


def random_trace(rnd, n):
    density = rnd.choice([0.2, 0.5, 0.8])
    return [{"a": int(rnd.random() < density), "b": int(rnd.random() < density),
             "c": int(rnd.random() < density), "d": int(rnd.random() < density),
             "x": rnd.randint(0, 2), "y": rnd.randint(0, 2)} for _ in range(n)]


class Degenerate(Exception):
    pass


def _compiled_flags(text):
    from cocotbext.sva._compile import Compiler
    from cocotbext.sva._parser import parse_assertion
    from cocotbext.sva._trace import TraceBinder
    prog = Compiler(TraceBinder(SIGNALS), text).compile(parse_assertion(text))
    return prog


def _vacuity_timed(text):
    """`or`/`iff`/`implies` below a `not`: the inner result waits until its
    vacuity is known (reference-simulator behaviour), which the formal model
    does not have; failure times of the negation can then differ by a tick."""
    from cocotbext.sva import _ast as A
    from cocotbext.sva._parser import parse_assertion

    def walk(n, under_not):
        if isinstance(n, A.PNot):
            under_not = True
        if under_not and isinstance(n, A.PBin) and n.op in ("or", "iff", "implies"):
            return True
        for v in vars(n).values() if isinstance(n, A.Node) else ():
            if isinstance(v, A.Node) and walk(v, under_not):
                return True
        return False
    return walk(parse_assertion(text).prop, False)


def engine_attempts(text, rows):
    """Per attempt start: ('fail', tick) / ('end_fail',) / ('ok',)."""
    prog = _compiled_flags(text)
    if _vacuity_timed(text):
        raise Degenerate()
    if prog.warnings or prog.contradiction:
        # never-matching sequences, or transitions dropped as unsatisfiable: the
        # verdict timing then follows the reference simulator, not the formal
        # semantics the reference interpreter implements
        raise Degenerate()
    res = run_trace({"p": text}, SIGNALS, rows)
    got = {s: ("ok",) for s in range(len(rows))}
    for e in res.events:
        if len(e.starts) != e.count:
            got[("merged", e.tick, e.count)] = (e.kind,)
            continue
        for s in e.starts:
            if e.kind == "fail":
                got[s] = ("fail", e.tick)
            elif e.kind == "end_fail":
                got[s] = ("end_fail",)
    return Counter(got.items())


def reference_attempts(text, rows):
    ref = Ref(text, rows)
    exp = {}
    for start, verdict, info in ref.run():
        if verdict is None:
            exp[start] = ("end_fail",) if info is False else ("ok",)
        elif verdict == FAIL:
            exp[start] = ("fail", info)
        else:
            exp[start] = ("ok",)
    return Counter(exp.items())


def check_one(text, rows):
    try:
        got = engine_attempts(text, rows)
    except SvaError:
        return "skipped"
    except Degenerate:
        return "degenerate"
    exp = reference_attempts(text, rows)
    if got != exp:
        only_e = {k: v for k, v in (got - exp).items()}
        only_r = {k: v for k, v in (exp - got).items()}
        raise AssertionError(f"mismatch for\n  {text}\n  trace={rows}\n  engine only: {only_e}\n"
                             f"  reference only: {only_r}")
    return "ok"


def _cases(seed, n, locals_ok, temporal=False):
    rnd = random.Random(seed)
    g = Gen(rnd, temporal=temporal)
    for _ in range(n):
        text = g.local_prop() if locals_ok else g.prop(rnd.randint(1, 3))
        yield text, random_trace(rnd, rnd.randint(4, 18))


N = int(os.environ.get("FUZZ_N", "300"))
SEED = int(os.environ.get("FUZZ_SEED", "1"))


@pytest.mark.parametrize("chunk", range(4))
def test_fuzz_properties(chunk):
    stats = Counter()
    for text, rows in _cases(SEED * 1000 + chunk, N // 4, False):
        stats[check_one(text, rows)] += 1
    assert stats["ok"] > 0.6 * (N // 4), stats


def test_fuzz_local_variables():
    stats = Counter()
    for text, rows in _cases(SEED * 1000 + 99, max(50, N // 4), True):
        stats[check_one(text, rows)] += 1
    assert stats["ok"] > 0.8 * max(50, N // 4), stats


@pytest.mark.parametrize("chunk", range(4))
def test_fuzz_temporal(chunk):
    stats = Counter()
    for text, rows in _cases(SEED * 1000 + 500 + chunk, N // 4, False, temporal=True):
        stats[check_one(text, rows)] += 1
    assert stats["ok"] > 0.6 * (N // 4), stats
