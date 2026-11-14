"""Replay the golden corpus: engine results must match the reference results
recorded from a mature SVA implementation (see devtools/corpus/)."""
import json
from collections import Counter
from pathlib import Path

import pytest

from cocotbext.sva._trace import run_trace

CORPUS = Path(__file__).parent / "corpus" / "corpus.json"
DATA = json.loads(CORPUS.read_text()) if CORPUS.exists() else {"cases": [], "signals": {}}

# Known, documented differences: case id -> reason.
_QUIRK = ("reference tool reports a vacuous success for `if (c) P else (S |=> Q)` when c is true "
          "and P fails (IEEE 1800 16.12.8: the result is P's); confirmed in isolation")
_SEMANTIC_UNSAT = ("reference tool drops transitions whose combined conditions are unsatisfiable "
                   "across different expressions (e.g. a with !(a && c)); the engine only detects "
                   "contradictions on the same condition, so it fails one tick later")
_DOUBLE = ("reference tool reports two verdicts for some attempts (passes + failures exceed the "
           "attempt count and its vacuous counter underflows); every engine verdict is one of them")
_UNSAT_EARLY = ("the engine proves at compile time that the antecedent can never match (contradictory "
                "conditions on the same expression) and resolves attempts at once; the reference tool "
                "keeps 5 attempts pending to the end, as the formal semantics does")
_NOT_AND_END = ("end of test: `not (P and Q)` with Q held and P a pending weak obligation; the engine "
                "and the reference interpreter (formal semantics: P holds, so the negation fails) "
                "report a failure, the reference tool does not")
_NOT_AND_END_INTERSECT = (_NOT_AND_END + "; also, the reference tool notices one tick later that "
                          "an `intersect` can no longer match")
_ALWAYS_SEQ = ("`always [m:n] S` with a multi-tick sequence S: the reference tool's verdicts are not "
               "consistent with any reading (attempts with identical instance outcomes get different "
               "verdicts); boolean and implication operands of `always` match exactly")
_ONCE_ALWAYS = ("single-attempt assertion `always P` with P an implication or s_eventually: the "
                "reference tool ends the evaluation after P's first evaluation; with boolean P it "
                "evaluates every tick (matches)")
_ONCE_END = ("single-attempt assertion: the reference tool never reports pending strong obligations "
             "at the end of the simulation; the engine reports them as for any assertion")
_END_STRONG = ("end of test: the engine (and the formal semantics) report a strong obligation still "
               "pending (s_nexttime / s_eventually / s_always / s_until, or a strong sequence whose |=> "
               "consequent has not started); the reference tool does not report it for these "
               "top-level forms, although it does inside implications and for top-level strong "
               "sequences")
_END_DOUBLE = ("end of test: the reference tool reports pending obligations of some attempts twice "
               "(duplicate start times) that the engine and the formal semantics consider held")
_WITHIN1 = ("`S1 within S2` with single-tick operands on the same tick: the reference tool never "
            "matches; the formal semantics and the engine do")
_LATE = "the reference tool notices one tick later that a sequence can no longer match"
_CONST_FALSE = ("a constant-false condition (the !1 of `1[=n]`) is pruned at compile time; the "
                "reference tool resolves those attempts one tick later")
_VAC_OR = ("vacuity of `or` with a reject_on/if operand: the reference tool counts nonvacuous "
           "successes where the engine counts vacuous ones (verdict times agree)")
KNOWN = {
    "s0022": _ALWAYS_SEQ, "s0040": _ALWAYS_SEQ, "s0048": _ALWAYS_SEQ,
    "s0060": _END_STRONG, "s0068": _END_STRONG, "s0085": _END_STRONG, "s0086": _END_STRONG,
    "s0089": _END_STRONG, "s0092": _END_STRONG, "s0097": _END_STRONG, "s0109": _END_STRONG,
    "s0130": _END_STRONG, "s0131": _END_STRONG, "s0148": _END_STRONG,
    "s0042": _END_DOUBLE, "s0063": _END_DOUBLE,
    "s0001": _WITHIN1, "s0034": _LATE, "s0073": _CONST_FALSE, "s0115": _CONST_FALSE,
    "s0120": _VAC_OR,
    "r0148": _QUIRK, "r0252": _QUIRK, "r0265": _SEMANTIC_UNSAT,
    "r0032": _DOUBLE, "r0274": _DOUBLE, "r0040": _UNSAT_EARLY,
    "r0143": _NOT_AND_END, "r0296": _NOT_AND_END_INTERSECT,
    "t009_0": _ALWAYS_SEQ, "t009_1": _ALWAYS_SEQ, "t009_2": _ALWAYS_SEQ,
    "o000_0": _ONCE_ALWAYS, "o000_1": _ONCE_ALWAYS, "o000_2": _ONCE_ALWAYS,
    "o008_0": _ONCE_ALWAYS + "; " + _ONCE_END, "o008_1": _ONCE_ALWAYS, "o008_2": _ONCE_ALWAYS,
    "o010_0": _ONCE_ALWAYS, "o010_1": _ONCE_ALWAYS, "o010_2": _ONCE_ALWAYS,
    "o007_0": _ONCE_END, "o007_1": _ONCE_END, "o007_2": _ONCE_END, "o009_1": _ONCE_END,
}


def engine_result(case):
    # The reference also samples the first clock edge, before row 0 is driven
    # (all signals at their initial value 0): replay it as tick 0.
    zeros = {s: 0 for s in DATA["signals"]}
    r = run_trace(case["props"], DATA["signals"], [zeros] + case["trace"], kinds=case["kinds"],
                  initial=zeros, once=case.get("once", ()))
    out = {}
    for name, kind in case["kinds"].items():
        fails, end_fails, passes = [], [], []
        for e in r.events:
            if e.assertion != name:
                continue
            # the engine keeps at most MAX_STARTS start times per merged report;
            # the missing ones are wildcards (WILD) in the comparison
            starts = list(e.starts) + [WILD] * (e.count - len(e.starts))
            if e.kind == "fail":
                fails += [(e.tick, s) for s in starts]
            elif e.kind == "end_fail":
                end_fails += starts
            elif e.kind == "pass":
                passes += [e.tick] * e.count
        st = r.stats[name]
        out[name] = {"fails": sorted(fails), "end_fails": sorted(end_fails),
                     "passes": sorted(passes) if kind != "cover" else [],
                     "covers": sorted(passes) if kind == "cover" else [],
                     # the reference's counts are taken before end-of-test failures
                     "counts": {"attempts": st["attempts"], "vacuous": st["vacuous"],
                                "disabled": st["disabled"], "passes": st["passes"],
                                "failures": st["failures"] - st["pending_fail"]}}
    return out


WILD = -1


def _match_wildcards(a: Counter, b: Counter):
    """Pair engine entries with an unknown start time with leftover reference
    entries (same failure tick for `fails`)."""
    def wild(x):
        return x == WILD or (isinstance(x, tuple) and x[1] == WILD)
    if not any(wild(x) for x in a):
        return a, b
    common = a & b
    a, b = a - common, b - common
    for x in [x for x in a.elements() if wild(x)]:
        cand = [y for y in b.elements() if x == WILD or y[0] == x[0]]
        if cand:
            a[x] -= 1
            b[cand[0]] -= 1
    return +a, +b


def compare(case):
    got = engine_result(case)
    diffs = []
    for name, ref in case["ref"].items():
        g = got[name]
        for key in ("fails", "end_fails", "passes", "covers"):
            a, b = Counter(map(tuple, g[key]) if key == "fails" else g[key]), \
                Counter(map(tuple, ref[key]) if key == "fails" else ref[key])
            a, b = _match_wildcards(a, b)
            if a != b:
                diffs.append(f"{name}.{key}: engine-only {dict(a - b)} reference-only {dict(b - a)}")
        rc = ref.get("counts")
        if rc and case["kinds"][name] != "cover":
            for k in ("attempts", "vacuous", "disabled", "passes", "failures"):
                if rc[k] > rc["attempts"]:
                    continue        # implausible reference count (seen once: 2**32 - 10)
                if g["counts"][k] != rc[k]:
                    diffs.append(f"{name}.{k}: engine {g['counts'][k]} reference {rc[k]}")
    return diffs


@pytest.mark.skipif(not DATA["cases"], reason="no corpus")
@pytest.mark.parametrize("case", DATA["cases"], ids=[c["id"] for c in DATA["cases"]])
def test_corpus_case(case):
    if case["id"] in KNOWN:
        pytest.xfail(KNOWN[case["id"]])
    diffs = compare(case)
    assert not diffs, f"{case['props']}\n" + "\n".join(diffs)
