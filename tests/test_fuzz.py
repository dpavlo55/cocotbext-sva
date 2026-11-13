"""Differential fuzzing: engine vs. reference interpreter on random properties.

    pytest tests/test_fuzz.py                 # fixed seeds
    FUZZ_N=5000 FUZZ_SEED=7 pytest tests/test_fuzz.py -x
"""
import os
import random
from collections import Counter

import pytest

from cocotbext.sva._errors import SvaError
from cocotbext.sva._trace import run_trace
from refsem import FAIL, PASS, PASS_ANY, VPASS, Ref

SIGNALS = {"a": 1, "b": 1, "c": 1, "d": 1, "x": 3, "y": 3}


class Gen:
    def __init__(self, rnd, locals_ok=False):
        self.r = rnd
        self.locals_ok = locals_ok

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


def engine_attempts(text, rows):
    res = run_trace({"p": text}, SIGNALS, rows)
    if res.warnings:      # a sequence that can never match: verdict timing is implementation-defined
        raise Degenerate()
    got = Counter()
    for e in res.events:
        kind = {"fail": FAIL, "pass": PASS, "vpass": VPASS, "end_fail": "end_fail"}[e.kind]
        if len(e.starts) == e.count:
            for s in e.starts:
                got[(s, kind, e.tick if kind != "end_fail" else None)] += 1
        else:
            got[("merged", kind, e.tick, e.count)] += 1
    return got


def reference_attempts(text, rows):
    ref = Ref(text, rows)
    exp = Counter()
    for start, verdict, info in ref.run():
        if verdict is None:
            if info is False:
                exp[(start, "end_fail", None)] += 1
        else:
            exp[(start, verdict, info)] += 1
    return exp


def check_one(text, rows):
    try:
        got = engine_attempts(text, rows)
    except SvaError:
        return "skipped"
    except Degenerate:
        return "degenerate"
    exp = reference_attempts(text, rows)
    for key in [k for k in exp if k[1] == PASS_ANY]:
        start, _, tick = key
        for kind in (PASS, VPASS):
            if got.get((start, kind, tick)):
                got[(start, kind, tick)] -= 1
                got[key] += 1
                break
    got = +got
    if got != exp:
        only_e = {k: v for k, v in (got - exp).items()}
        only_r = {k: v for k, v in (exp - got).items()}
        raise AssertionError(f"mismatch for\n  {text}\n  trace={rows}\n  engine only: {only_e}\n"
                             f"  reference only: {only_r}")
    return "ok"


def _cases(seed, n, locals_ok):
    rnd = random.Random(seed)
    g = Gen(rnd)
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
