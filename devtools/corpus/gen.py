"""Golden-corpus case generator.

Writes build/corpus/cases.json and build/corpus/tb.sv. The testbench is plain
IEEE 1800 SystemVerilog: every case is a module that replays its trace (driven
on the falling clock edge) and checks its properties with `assert property`
(pass action prints "@@PASS <scope> <time>") or `cover property` ("@@COVER").
Failures use the simulator's default failure message, which carries the
failure and the attempt start time.

Timing: clock period 10, first rising edge at 5 (signals at their initial
value 0: tick 0). Row k of a trace is sampled at time 10*k + 15 (tick k + 1).
The simulation ends after the last row.

    python devtools/corpus/gen.py [--random N] [--seed S] [--all]

By default only cases whose id is not yet in tests/corpus/corpus.json are
written (the merge step appends them); --all regenerates everything. The
random phase 1 cases are not reproducible (the usability filter depends on
the engine version), so --all replaces them.
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


def _usable(text: str, kind: str) -> bool:
    """Valid for the engine and not degenerate (never-matching sequences)."""
    try:
        r = run_trace({"p": text}, SIGNALS, [], kinds={"p": kind})
    except SvaError:
        return False
    return not r.warnings

N_ROWS = 24
SIGNALS = {"a": 1, "b": 1, "c": 1, "d": 1, "rst": 1, "x": 3, "y": 3}

DIRECTED = [
    # implication / delays / repetition
    "a |-> ##[1:3] b", "a |=> b ##1 c", "a ##1 b |-> c", "a |-> ##[0:2] b", "##2 a |-> b",
    "a |-> b[*2] ##1 c", "a |-> b[*1:$] ##1 c", "a[*0:2] ##1 b |-> c", "a |-> b[->2] ##1 c",
    "a |-> b[=2] ##1 c", "a |-> b[->1:2]", "a |-> ##[2:$] b", "a ##[*] b |-> c", "a ##[+] b |=> c",
    "(a ##0 b) |-> c ##0 d",
    # sequence operators
    "a |-> (b ##1 c) or (c ##2 d)", "a |-> (b ##1 c) and (c[*3])", "a |-> (b[*2]) intersect (c ##1 d)",
    "a |-> (b ##1 c) within (d[*4])", "a |-> b throughout (c ##2 d)", "a |-> first_match(b ##[1:3] c)",
    "first_match(a ##[0:2] b) |=> c",
    # property operators
    "a ##[1:2] b |=> not c", "not (a ##1 b)", "(a |-> b) and (c |-> d)", "(a |-> b) or (c |=> d)",
    "if (a) b ##1 c else d", "if (a) b |=> c", "case (x) 0: a; 1, 2: b ##1 c; default: d; endcase",
    "a #-# b ##1 c", "a #=# c", "(a ##1 b) implies c", "(a |-> b) iff c",
    # strength
    "a |-> strong(##[1:$] b)", "a |-> ##[1:$] b", "a |-> weak(b ##1 c)", "strong(a ##[1:3] b)",
    # sampled-value functions and expressions
    "$rose(a) |=> b", "$fell(a) |-> c", "$stable(x) |-> !b", "$changed(x) |=> c",
    "a |-> x == $past(x) + 3'd1", "a |-> x == $past(y, 2)", "b |-> $past(a, 1, c)",
    "$onehot(x) |-> a", "$countones(x) >= 2 |=> b", "a |-> x[1:0] != y[2:1]", "x + y == 3'd7 |-> d",
    # local variables
    "(a, v = x) |-> ##2 y == v", "(a, v = x) |-> ##[1:3] (b && y == v)",
    "(a, v = x) |=> (c, v = v + 3'd1) ##1 y == v", "(a ##1 b, v = x) |-> ##1 (y == v)[*2]",
    # disable iff
    "disable iff (rst) a |-> ##[1:3] b", "disable iff (rst) a |=> b[*2]",
]
DIRECTED_COVERS = ["a ##1 b ##1 c", "a ##[1:3] b", "a[*2] ##1 !a", "(a, v = x) ##1 y == v"]

# Phase 2: temporal property operators. Appended with their own random stream
# so that the phase 1 cases above stay unchanged.
DIRECTED2 = [
    # nexttime
    "a |-> nexttime b", "a |-> nexttime[2] b", "a |-> s_nexttime b", "a |-> s_nexttime[2] b",
    "a |-> nexttime[0] b", "a |-> nexttime (b ##1 c)", "a |=> s_nexttime (b |-> c)",
    # always
    "a |-> always [1:3] b", "a |-> s_always [1:3] b", "a |-> always [0:2] (b ##1 c)",
    "a |-> always [2:$] b", "a |-> always b", "a |-> always (b |-> ##1 c)",
    "a |-> s_always [0:2] (b |=> c)",
    # eventually
    "a |-> eventually [1:3] b", "a |-> eventually [0:2] (b ##1 c)", "a |-> s_eventually b",
    "a |-> s_eventually [1:3] b", "a |-> s_eventually [2:$] b", "a |-> s_eventually (b ##1 c)",
    "a |-> s_eventually (b |-> c)", "a |-> eventually [1:2] (b |=> c)",
    # until
    "a |-> b until c", "a |-> b s_until c", "a |-> b until_with c", "a |-> b s_until_with c",
    "a |-> (b ##1 c) until d", "a |-> b until (c ##1 d)", "a |-> (b |-> c) until d",
    "a |-> b s_until (c ##1 d)",
    # aborts
    "accept_on (c) a |-> ##[1:3] b", "reject_on (c) a |-> ##[1:3] b",
    "sync_accept_on (c) a |=> b ##1 b", "sync_reject_on (c) a |=> b[*2]",
    "a |-> accept_on (c) b[*3]", "a |-> reject_on (c) ##2 b", "accept_on (c) reject_on (d) a |=> b",
    "a |-> sync_reject_on (c) s_eventually b", "a |-> accept_on (c) b until d",
    # combinations
    "not (a |-> eventually [1:2] b)", "a |-> not (b until c)", "a |-> not (s_eventually [0:2] b)",
    "(a |-> always [0:2] b) or (c |-> s_eventually d)", "(a |-> nexttime b) and (c |-> b until d)",
    "if (a) always [0:1] b else s_nexttime c", "a |-> (b until c) implies d",
    "disable iff (rst) a |-> s_eventually b", "disable iff (rst) a |-> always [0:3] !c",
    # empty antecedent matches (nested implications)
    "a[*0:1] |=> b", "a |-> b[*0:1] |=> c", "a[*0:1] |-> b", "a |-> (b[*0:2] |-> c)",
    "a |-> b[*0:1] |-> c", "a |=> b[*0:1] |=> c",
]
N_RANDOM2 = 150      # random properties with phase 2 operators

# single-attempt assertions (SV: concurrent assertion in an initial block)
DIRECTED2_ONCE = [
    "always (a |-> ##[1:2] b)", "s_eventually (a && b)", "a until b", "a s_until b",
    "always [2:$] (a || b)", "s_always [0:5] !rst", "nexttime[3] a", "s_nexttime[30] a",
    "always s_eventually a", "s_eventually always !rst", "always (a |=> s_eventually b)",
    "eventually [2:4] (a ##1 b)", "a until_with b", "always [0:$] (a -> b)",
]
DIRECTED2_COVERS = ["a #=# s_eventually b", "b until c", "a |-> always [0:1] b", "s_eventually (a ##1 b)"]


def random_trace(rnd: random.Random, n: int) -> list:
    dens = rnd.choice([0.2, 0.4, 0.6])
    rows = []
    for _ in range(n):
        rows.append({
            "a": int(rnd.random() < dens), "b": int(rnd.random() < dens),
            "c": int(rnd.random() < dens), "d": int(rnd.random() < dens),
            "rst": int(rnd.random() < 0.06), "x": rnd.randint(0, 7), "y": rnd.randint(0, 7),
        })
    return rows


def make_cases(n_random: int, seed: int) -> list:
    rnd = random.Random(seed)
    cases = []
    for i, text in enumerate(DIRECTED):
        for k in range(3):
            cases.append({"id": f"d{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                          "trace": random_trace(rnd, N_ROWS)})
    for i, text in enumerate(DIRECTED_COVERS):
        for k in range(2):
            cases.append({"id": f"c{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "cover"},
                          "trace": random_trace(rnd, N_ROWS)})
    g = Gen(rnd)
    i = 0
    while i < n_random:
        text = g.local_prop() if rnd.random() < 0.15 else g.prop(rnd.randint(1, 3))
        if not _usable(text, "assert"):
            continue
        i += 1
        cases.append({"id": f"r{i:04d}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                      "trace": random_trace(rnd, N_ROWS)})
    rnd2 = random.Random(seed + 1)
    for i, text in enumerate(DIRECTED2):
        for k in range(3):
            cases.append({"id": f"t{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                          "trace": random_trace(rnd2, N_ROWS)})
    for i, text in enumerate(DIRECTED2_ONCE):
        for k in range(3):
            cases.append({"id": f"o{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                          "once": ["p0"], "trace": random_trace(rnd2, N_ROWS)})
    g2 = Gen(rnd2, temporal=True)
    i = 0
    while i < N_RANDOM2:
        text = g2.prop(rnd2.randint(1, 3))
        if not _usable(text, "assert"):
            continue
        i += 1
        cases.append({"id": f"s{i:04d}", "props": {"p0": text}, "kinds": {"p0": "assert"},
                      "trace": random_trace(rnd2, N_ROWS)})
    for i, text in enumerate(DIRECTED2_COVERS):
        for k in range(2):
            cases.append({"id": f"u{i:03d}_{k}", "props": {"p0": text}, "kinds": {"p0": "cover"},
                          "trace": random_trace(rnd2, N_ROWS)})
    return cases


def emit_sv(cases: list) -> str:
    out = ["// Generated by devtools/corpus/gen.py -- do not edit.", "`timescale 1ns/1ns", ""]
    for idx, case in enumerate(cases):
        n = len(case["trace"])
        out.append(f"module case_{idx} (input logic clk, input int cyc);")
        out.append("  logic a = 0, b = 0, c = 0, d = 0, rst = 0;")
        out.append("  logic [2:0] x = 0, y = 0;")
        for sig, w in SIGNALS.items():
            vals = ", ".join(f"{w}'d{row[sig]}" for row in case["trace"])
            out.append(f"  localparam logic [{w - 1}:0] T_{sig} [{n}] = '{{{vals}}};")
        drives = " ".join(f"{s} <= T_{s}[cyc];" for s in SIGNALS)
        # $time > 0: the clock port's X->0 change at time 0 is a negedge too
        out.append(f"  always @(negedge clk) if ($time > 0 && cyc < {n}) begin {drives} end")
        for name, text in case["props"].items():
            body = f"@(posedge clk) {text}"
            if re.search(r"v\s*=(?!=)", text):
                # SV needs local variables declared in a named property
                out.append(f"  property {name}_p; logic [2:0] v; {body}; endproperty")
                body = f"{name}_p"
            initial = "initial " if name in case.get("once", ()) else ""
            if case["kinds"][name] == "cover":
                out.append(f"  {initial}{name}: cover property ({body}) "
                           f"$display(\"@@COVER %m %0t\", $time);")
            else:
                out.append(f"  {initial}{name}: assert property ({body}) "
                           f"$display(\"@@PASS %m %0t\", $time);")
        out.append("endmodule")
        out.append("")
    out.append("module tb;")
    out.append("  logic clk = 0;")
    out.append("  int cyc = 0;")
    out.append("  always #5 clk = ~clk;")
    out.append(f"  always @(negedge clk) begin if (cyc == {N_ROWS}) $finish; cyc <= cyc + 1; end")
    for idx, case in enumerate(cases):
        out.append(f"  case_{idx} u_{idx} (.clk(clk), .cyc(cyc));")
    out.append("endmodule")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=300)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--all", action="store_true", help="regenerate every case")
    ap.add_argument("--out", default=str(ROOT / "build" / "corpus"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = make_cases(args.random, args.seed)
    existing = ROOT / "tests" / "corpus" / "corpus.json"
    if not args.all and existing.exists():
        have = {c["id"] for c in json.loads(existing.read_text())["cases"]}
        cases = [c for c in cases if c["id"] not in have]
    for idx, c in enumerate(cases):
        c["module"] = f"u_{idx}"
    (out / "cases.json").write_text(json.dumps({"rows": N_ROWS, "signals": SIGNALS, "cases": cases,
                                             "all": args.all}))
    (out / "tb.sv").write_text(emit_sv(cases))
    print(f"{len(cases)} cases -> {out}")


if __name__ == "__main__":
    main()
