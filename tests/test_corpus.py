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
_OPEN = "not analysed yet: nested vacuity / end-of-test combination (phase 2)"
KNOWN = {
    "r0148": _QUIRK, "r0252": _QUIRK, "r0265": _SEMANTIC_UNSAT,
    "r0032": _OPEN, "r0040": _OPEN, "r0112": _OPEN, "r0143": _OPEN, "r0245": _OPEN,
    "r0274": _OPEN, "r0296": _OPEN,
}


def engine_result(case):
    # The reference also samples the first clock edge, before row 0 is driven
    # (all signals at their initial value 0): replay it as tick 0.
    zeros = {s: 0 for s in DATA["signals"]}
    r = run_trace(case["props"], DATA["signals"], [zeros] + case["trace"], kinds=case["kinds"],
                  initial=zeros)
    out = {}
    for name, kind in case["kinds"].items():
        fails, end_fails, passes = [], [], []
        for e in r.events:
            if e.assertion != name:
                continue
            starts = list(e.starts) + [e.starts[-1]] * (e.count - len(e.starts))
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


def compare(case):
    got = engine_result(case)
    diffs = []
    for name, ref in case["ref"].items():
        g = got[name]
        for key in ("fails", "end_fails", "passes", "covers"):
            a, b = Counter(map(tuple, g[key]) if key == "fails" else g[key]), \
                Counter(map(tuple, ref[key]) if key == "fails" else ref[key])
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
