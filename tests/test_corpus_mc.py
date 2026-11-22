"""Replay the multi-clock golden corpus: engine results must match the
reference results recorded from a mature SVA implementation
(devtools/corpus/gen_mc.py)."""
import json
from collections import Counter
from pathlib import Path

import pytest

from cocotbext.sva import _engine
from cocotbext.sva._trace import run_trace

_engine.MAX_STARTS = 1 << 30

CORPUS = Path(__file__).parent / "corpus" / "corpus_mc.json"
DATA = json.loads(CORPUS.read_text()) if CORPUS.exists() else {"cases": [], "signals": {}, "clocks": []}

# Known, documented differences: reason -> case ids.
_UNALIGNED = (
    "a property operator (nexttime, always, eventually, until, not, #-#) that follows a clock "
    "change starts between two ticks of its own clock. The engine follows the clock rewriting of "
    "the formal semantics: the operator is evaluated from the next tick of its clock on (nexttime "
    "then moves one tick further, always [0:n] starts counting there). The reference tool counts "
    "from the time of the antecedent's last tick instead (nexttime b is checked at the first tick "
    "of the new clock, like b alone), and it decides `not (b ##1 c)` at the antecedent's tick, "
    "when the new clock has not ticked yet. Sequences, implications and if/else after a clock "
    "change match exactly")
_END_STRONG = ("end of test: the engine reports a strong sequence that is still pending; the "
               "reference tool does not for this top-level form (as in the single-clock corpus)")
_PAST_X = ("$past before enough ticks: the reference returns X (the testbench signals have no "
           "initial value), so the comparison fails; the engine has no X history and returns 0")
_LATE = "the reference tool notices one tick later that a sequence can no longer match"
_KNOWN = {
    _UNALIGNED: [
        "md023_0", "md023_1", "md024_0", "md024_1", "md024_2", "md025_0", "md025_1", "md025_2",
        "md026_0", "md026_1", "md026_2", "md027_0", "md027_1", "md027_2", "md028_0", "md028_1",
        "md028_2", "md030_1", "md030_2", "md031_0", "md031_1", "md031_2", "md033_0", "md033_1",
        "md033_2", "mr0002", "mr0043", "mr0079", "mr0098", "mr0100", "mr0104", "mr0105", "mr0108",
        "mr0118", "mr0121", "mr0147", "mr0182", "mr0188", "mr0189"],
    _END_STRONG: ["md044_1"],
    _PAST_X: ["md049_0"],
    _LATE: ["mr0042", "mr0096"],
}
KNOWN = {cid: reason for reason, ids in _KNOWN.items() for cid in ids}


def engine_result(case):
    clocks = DATA["clocks"]
    steps = [k for k, row in enumerate(case["trace"]) if any(row[c] for c in clocks)]
    rows = [case["trace"][k] for k in steps]
    r = run_trace(case["props"], DATA["signals"], rows, kinds=case["kinds"])
    out = {}
    for name, kind in case["kinds"].items():
        fails, end_fails, passes = [], [], []
        for e in r.events:
            if e.assertion != name:
                continue
            starts = [steps[s] for s in e.starts]
            if e.kind == "fail":
                fails += [(steps[e.tick], s) for s in starts]
            elif e.kind == "end_fail":
                end_fails += starts
            elif e.kind == "pass":
                passes += [steps[e.tick]] * e.count
        st = r.stats[name]
        out[name] = {"fails": sorted(fails), "end_fails": sorted(end_fails),
                     "passes": sorted(passes) if kind != "cover" else [],
                     "covers": sorted(passes) if kind == "cover" else [],
                     "counts": {"attempts": st["attempts"], "vacuous": st["vacuous"],
                                "disabled": st["disabled"], "passes": st["passes"],
                                "failures": st["failures"] - st["pending_fail"]}}
    return out


def compare(case, counts=True):
    got = engine_result(case)
    diffs = []
    for name, ref in case["ref"].items():
        g = got[name]
        for key in ("fails", "end_fails", "passes", "covers"):
            a = Counter(map(tuple, g[key]) if key == "fails" else g[key])
            b = Counter(map(tuple, ref[key]) if key == "fails" else ref[key])
            if a != b:
                diffs.append(f"{name}.{key}: engine-only {dict(a - b)} reference-only {dict(b - a)}")
        rc = ref.get("counts")
        if counts and rc and case["kinds"][name] != "cover":
            for k in ("attempts", "vacuous", "disabled", "passes", "failures"):
                if rc[k] > rc["attempts"]:
                    continue        # implausible reference count
                if g["counts"][k] != rc[k]:
                    diffs.append(f"{name}.{k}: engine {g['counts'][k]} reference {rc[k]}")
    return diffs


@pytest.mark.skipif(not DATA["cases"], reason="no corpus")
@pytest.mark.parametrize("case", DATA["cases"], ids=[c["id"] for c in DATA["cases"]])
def test_corpus_mc_case(case):
    if case["id"] in KNOWN:
        pytest.xfail(KNOWN[case["id"]])
    diffs = compare(case)
    assert not diffs, f"{case['props']}\n" + "\n".join(diffs)
