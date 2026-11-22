"""Golden-corpus case generator for multi-clock properties.

Writes build/corpus_mc/cases.json and build/corpus_mc/tb.sv (plain IEEE 1800
SystemVerilog, see gen.py). Every case is a module with its own clocks.

Timing: step k is at time 10*k + 5. A clock that ticks in step k rises there
(clocks that tick in the same step rise in the same time step); all clocks
fall at 10*k + 10, where the signals for step k + 1 are driven. A case's
trace is the list of its steps: the values sampled in the step and, under the
clock names, whether each clock ticks. Steps without any tick are kept in the
file (the engine skips them). The simulation ends after the last step.

    python devtools/corpus/gen_mc.py [--random N] [--seed S]
    python local/corpus/run_mc.py          # reference simulator (local only)
    python devtools/corpus/gen_mc.py --merge
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from test_fuzz import Gen  # noqa: E402

from cocotbext.sva._errors import SvaError  # noqa: E402
from cocotbext.sva._trace import run_trace  # noqa: E402

N_STEPS = 24
CLOCKS = ("c0", "c1", "c2")
SIGNALS = {"a": 1, "b": 1, "c": 1, "d": 1, "x": 3, "y": 3}
DEST = ROOT / "tests" / "corpus" / "corpus_mc.json"

P0, P1, P2 = "@(posedge c0)", "@(posedge c1)", "@(posedge c2)"

DIRECTED = [
    # concatenation and fusion across clocks
    f"{P0} a ##1 {P1} b", f"{P0} a ##0 {P1} b", f"{P0} a ##1 {P1} b ##1 {P2} c",
    f"{P0} a ##1 b ##1 {P1} c ##1 d", f"{P0} a ##[1:2] b ##1 {P1} c", f"{P0} a[*2] ##1 {P1} b[*2]",
    f"{P0} a ##1 {P1} b ##1 {P0} c", f"{P0} (a ##1 {P1} b) or (c ##1 {P1} d)",
    # implications
    f"{P0} a |=> {P1} b", f"{P0} a |-> {P1} b", f"{P0} a ##1 b |=> {P1} c ##1 d",
    f"{P0} a |=> {P1} b ##1 {P2} c", f"{P0} a ##1 {P1} b |=> c", f"{P0} a ##1 {P1} b |-> c",
    f"{P0} a ##1 {P1} b |=> {P0} c", f"{P0} a |-> {P1} b ##[1:2] c", f"{P0} a |=> {P1} b[*1:2] ##1 c",
    f"{P0} a |-> ##1 b ##1 {P1} c", f"{P0} a |=> {P1} b |=> {P2} c", f"{P0} a |-> {P1} b |-> {P2} c",
    f"{P0} a #=# {P1} b", f"{P0} a #-# {P1} b",
    # property operators under another clock
    f"{P0} a |=> {P1} not b", f"{P0} a |-> {P1} not (b ##1 c)", f"{P0} a |=> {P1} nexttime b",
    f"{P0} a |=> {P1} nexttime[2] b", f"{P0} a |-> {P1} s_nexttime b", f"{P0} a |=> {P1} always [0:2] b",
    f"{P0} a |-> {P1} always [1:2] b", f"{P0} a |=> {P1} s_eventually b",
    f"{P0} a |-> {P1} eventually [0:2] b", f"{P0} a |=> {P1} s_eventually [1:3] b",
    f"{P0} a |=> {P1} (b until c)", f"{P0} a |-> {P1} (b s_until c)", f"{P0} a |=> {P1} (b until_with c)",
    f"{P0} a |=> {P1} if (b) c else d", f"{P0} a |-> {P1} if (b) (c ##1 d)",
    f"{P0} a |=> ({P1} b) and ({P2} c)", f"{P0} a |=> ({P1} b ##1 c) or ({P2} d)",
    f"{P0} a |-> ({P1} b) and ({P0} c ##1 d)", f"{P0} a |=> {P1} strong(b ##[1:$] c)",
    f"{P0} a |=> {P1} (b |=> c)", f"{P0} a |=> {P1} (b |-> {P2} c)",
    # strength and negation at the top
    f"not ({P0} a ##1 {P1} b)", f"strong({P0} a ##1 {P1} b ##1 c)",
    # sampled-value functions
    f"{P0} $rose(a) |=> {P1} b", f"{P0} a |=> {P1} $rose(b)", f"{P0} a |=> {P1} x == $past(x)",
    f"{P0} a |-> {P1} $stable(x)", f"{P0} $fell(a) ##1 {P1} $changed(x) |=> y == $past(y, 2)",
    # local variables
    f"{P0} (a, v = x) |=> {P1} y == v", f"{P0} (a, v = x) ##1 {P1} (b, v = v + 3'd1) |=> y == v",
]
DIRECTED_COVERS = [f"{P0} a ##1 {P1} b", f"{P0} a ##0 {P1} b ##1 {P2} c", f"{P0} a ##1 {P1} b[*2]"]


def random_prop(rnd: random.Random, g: Gen, gt: Gen) -> str:
    def clk():
        return f"@(posedge c{rnd.randint(0, len(CLOCKS) - 1)})"

    def s():
        return g.seq(1)

    k = rnd.randint(0, 11)
    if k == 0:
        return f"{clk()} {s()} ##{rnd.randint(0, 1)} {clk()} {s()}"
    if k == 1:
        return f"{clk()} {s()} |=> {clk()} {s()}"
    if k == 2:
        return f"{clk()} {s()} |-> {clk()} {s()}"
    if k == 3:
        return f"{clk()} {s()} ##1 {clk()} {s()} |=> {clk()} {s()}"
    if k == 4:
        return f"{clk()} {s()} ##0 {clk()} {s()} |-> {s()}"
    if k in (5, 6):
        return f"{clk()} {s()} {rnd.choice(['|->', '|=>'])} {clk()} ({gt.prop(1)})"
    if k == 7:
        return f"{clk()} {s()} |=> ({clk()} {s()}) {rnd.choice(['and', 'or'])} ({clk()} {s()})"
    if k == 8:
        return f"{clk()} {s()} |=> {clk()} not ({s()})"
    if k == 9:
        return f"{clk()} {s()} {rnd.choice(['#-#', '#=#'])} {clk()} {s()}"
    if k == 10:
        return f"{clk()} {s()} ##1 {clk()} {s()} ##1 {clk()} {s()}"
    return f"{clk()} {s()} |=> {clk()} {s()} ##1 {clk()} {s()}"


def _usable(text: str, kind: str) -> bool:
    try:
        r = run_trace({"p": text}, SIGNALS, [], kinds={"p": kind})
    except SvaError:
        return False
    return not r.warnings


def random_trace(rnd: random.Random, n: int) -> list:
    dens = rnd.choice([0.3, 0.5, 0.7])
    rate = [rnd.choice([0.3, 0.5, 0.8]) for _ in CLOCKS]
    rows = []
    for _ in range(n):
        row = {"a": int(rnd.random() < dens), "b": int(rnd.random() < dens),
               "c": int(rnd.random() < dens), "d": int(rnd.random() < dens),
               "x": rnd.randint(0, 3), "y": rnd.randint(0, 3)}
        for c, p in zip(CLOCKS, rate):
            row[c] = int(rnd.random() < p)
        rows.append(row)
    return rows


def make_cases(n_random: int, seed: int) -> list:
    rnd = random.Random(seed)
    cases = []
    for i, text in enumerate(DIRECTED):
        for k in range(3):
            cases.append({"id": f"md{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                          "trace": random_trace(rnd, N_STEPS)})
    for i, text in enumerate(DIRECTED_COVERS):
        for k in range(2):
            cases.append({"id": f"mc{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "cover"},
                          "trace": random_trace(rnd, N_STEPS)})
    g, gt = Gen(rnd), Gen(rnd, temporal=True)
    i = 0
    while i < n_random:
        text = random_prop(rnd, g, gt)
        if not _usable(text, "assert"):
            continue
        i += 1
        cases.append({"id": f"mr{i:04d}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                      "trace": random_trace(rnd, N_STEPS)})
    return cases


def emit_sv(cases: list) -> str:
    out = ["// Generated by devtools/corpus/gen_mc.py -- do not edit.", "`timescale 1ns/1ns", ""]
    for idx, case in enumerate(cases):
        tr = case["trace"]
        n = len(tr)
        out.append(f"module case_{idx};")
        out.append("  logic a, b, c, d;")
        out.append("  logic [2:0] x, y;")
        out.append("  logic c0 = 0, c1 = 0, c2 = 0;")
        for sig, w in list(SIGNALS.items()) + [(c, 1) for c in CLOCKS]:
            vals = ", ".join(f"{w}'d{row[sig]}" for row in tr)
            out.append(f"  localparam logic [{w - 1}:0] T_{sig} [{n}] = '{{{vals}}};")
        drive = " ".join(f"{s} = T_{s}[k];" for s in SIGNALS)
        rise = " ".join(f"{c} = T_{c}[k];" for c in CLOCKS)
        fall = " ".join(f"{c} = 0;" for c in CLOCKS)
        out.append("  initial begin")
        out.append(f"    for (int k = 0; k < {n}; k++) begin")
        out.append(f"      {drive}")
        out.append(f"      #5 {rise}")
        out.append(f"      #5 {fall}")
        out.append("    end")
        out.append("  end")
        for name, text in case["props"].items():
            body = text
            if re.search(r"v\s*=(?!=)", text):
                out.append(f"  property {name}_p; logic [2:0] v; {body}; endproperty")
                body = f"{name}_p"
            if case["kinds"][name] == "cover":
                out.append(f"  {name}: cover property ({body}) $display(\"@@COVER %m %0t\", $time);")
            else:
                out.append(f"  {name}: assert property ({body}) $display(\"@@PASS %m %0t\", $time);")
        out.append("endmodule")
        out.append("")
    out.append("module tb;")
    out.append(f"  initial #{10 * N_STEPS} $finish;")
    for idx in range(len(cases)):
        out.append(f"  case_{idx} u_{idx} ();")
    out.append("endmodule")
    return "\n".join(out) + "\n"


def merge(out: Path):
    meta = json.loads((out / "cases.json").read_text())
    res = json.loads((out / "results.json").read_text())
    cases = []
    for c in meta["cases"]:
        r = res.get(c["module"], {})
        ref = {}
        for name in c["kinds"]:
            e = r.get(name, {"fails": [], "end_fails": [], "passes": [], "covers": []})
            ref[name] = {"fails": sorted(map(tuple, e["fails"])), "end_fails": sorted(e["end_fails"]),
                         "passes": sorted(e["passes"]), "covers": sorted(e["covers"]),
                         "counts": e.get("counts")}
        cases.append({"id": c["id"], "props": c["props"], "kinds": c["kinds"], "trace": c["trace"],
                      "ref": ref})
    DEST.write_text(json.dumps({"steps": meta["steps"], "signals": meta["signals"],
                                "clocks": meta["clocks"], "cases": cases}, indent=0))
    print(f"{len(cases)} cases -> {DEST}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=200)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--merge", action="store_true", help="merge reference results into the corpus")
    ap.add_argument("--out", default=str(ROOT / "build" / "corpus_mc"))
    args = ap.parse_args()
    out = Path(args.out)
    if args.merge:
        merge(out)
        return
    out.mkdir(parents=True, exist_ok=True)
    cases = make_cases(args.random, args.seed)
    for idx, c in enumerate(cases):
        c["module"] = f"u_{idx}"
    (out / "cases.json").write_text(json.dumps({"steps": N_STEPS, "signals": SIGNALS,
                                             "clocks": list(CLOCKS), "cases": cases}))
    (out / "tb.sv").write_text(emit_sv(cases))
    print(f"{len(cases)} cases -> {out}")


if __name__ == "__main__":
    main()
