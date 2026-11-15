"""Realistic engine benchmark (no simulator): where does the time go?

A Python model of a pipelined request/response interface (valid/ready
handshake with back-pressure, responses 2-6 cycles later carrying the request
tag, a small FSM, a counter, reset at the start) checked by a typical
verification-IP assertion set: handshake stability, tag tracking with local
variables, FSM rules, sampled-value functions, covers, disable iff. A second,
half-rate clock domain checks a slow status interface.

    python benchmarks/realistic_bench.py            # pure Python and kernel (if installed)
    python benchmarks/realistic_bench.py --profile
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocotbext.sva import _api, _engine  # noqa: E402
from cocotbext.sva._api import AssertionHandle, _Domain  # noqa: E402
from cocotbext.sva._compile import Compiler  # noqa: E402
from cocotbext.sva._expr import SignalRef  # noqa: E402
from cocotbext.sva._parser import parse_assertion  # noqa: E402

WIDTHS = {"rst_n": 1, "valid": 1, "ready": 1, "data": 8, "tag": 4, "rsp_valid": 1, "rsp_tag": 4,
          "rsp_data": 8, "state": 2, "busy": 1, "done": 1, "inc": 1, "count": 8, "err": 1,
          "s_req": 1, "s_ack": 1, "s_level": 4}

FAST = [
    # handshake rules (one set per direction)
    "valid && !ready |=> valid",
    "valid && !ready |=> $stable(data)",
    "valid && !ready |=> $stable(tag)",
    "rsp_valid |=> !rsp_valid || rsp_tag != $past(rsp_tag)",
    "ready |-> !err",
    # FSM: 0 idle, 1 busy, 2 done
    "state == 0 && valid && ready |=> state == 1",
    "state == 1 |=> state == 1 || state == 2",
    "state == 2 |=> state == 0",
    "$rose(busy) |-> ##[1:8] done",
    "done |=> !busy",
    "busy |-> state == 1",
    "done |-> state == 2",
    # counter
    "inc && count != 8'd255 |=> count == $past(count) + 8'd1",
    "!inc |=> $stable(count)",
    # latency bounds
    "valid && ready |-> ##[2:6] rsp_valid",
    "rsp_valid |-> $past(valid && ready, 2) || $past(valid && ready, 3) || $past(valid && ready, 4)"
    " || $past(valid && ready, 5) || $past(valid && ready, 6)",
    "valid && ready ##1 valid && ready |-> ##[1:6] rsp_valid ##[1:6] rsp_valid",
    "!valid |-> !ready || 1'b1",
    "err |-> 1'b0",
    "$fell(rst_n) |=> state == 0",
]
LOCAL = [
    "(valid && ready, t = tag) |-> ##[2:6] rsp_valid && rsp_tag == t",
    "(valid && ready, t = tag, d = data) |-> ##[2:6] rsp_valid && rsp_tag == t && rsp_data == d + 8'd1",
    "(inc, c = count) |=> count == c + 8'd1 || c == 8'd255",
    "(state == 0 && valid && ready, t = tag) |-> ##[1:10] state == 2",
]
COVERS = [
    "valid && !ready ##1 valid && !ready ##1 valid && !ready",
    "valid && ready ##1 valid && ready ##1 valid && ready",
    "rsp_valid ##1 rsp_valid",
    "$rose(busy) ##[1:3] done",
]
SLOW = [
    "s_req |-> ##[1:4] s_ack",
    "s_ack |=> !s_ack",
    "s_level != 4'd15",
    "$rose(s_req) |-> s_level < 4'd15",
]


def stimulus(cycles: int, seed: int = 1):
    """Rows {signal: value} as sampled at each rising edge of the main clock;
    the slow interface changes only on even cycles (its clock)."""
    rnd = random.Random(seed)
    rows, pend = [], []          # in-order responses: (due cycle, tag, data)
    last_due = 0
    valid = data = tag = 0
    state = busy = done = 0
    left = 0                     # busy cycles left
    count = inc = 0
    s_req = s_ack = s_level = 0
    s_wait = 0
    for cyc in range(cycles):
        rst_n = int(cyc >= 5)
        ready = int(rnd.random() < 0.7)
        rsp = pend.pop(0) if pend and pend[0][0] == cyc else None
        rows.append({"rst_n": rst_n, "valid": valid, "ready": ready, "data": data, "tag": tag,
                     "rsp_valid": int(rsp is not None), "rsp_tag": rsp[1] if rsp else 0,
                     "rsp_data": (rsp[2] + 1) & 0xFF if rsp else 0, "state": state, "busy": busy,
                     "done": done, "inc": inc, "count": count, "err": 0,
                     "s_req": s_req, "s_ack": s_ack, "s_level": s_level})
        # next values, from this cycle's values
        hs = valid and ready
        if hs:
            last_due = max(last_due + 1, cyc + rnd.randint(2, 4))
            pend.append((last_due, tag, data))
        if state == 0:
            if hs:
                state, left = 1, rnd.randint(1, 6)
        elif state == 1:
            left -= 1
            if left == 0:
                state = 2
        else:
            state = 0
        busy, done = int(state == 1), int(state == 2)
        if not valid or ready:
            valid = int(rst_n and rnd.random() < 0.35)
            data, tag = rnd.randrange(256), (tag + 1) & 15
        count = (count + 1) & 0xFF if inc else count
        inc = int(rnd.random() < 0.5)
        if cyc % 2 == 1:         # slow clock edge between this and the next even cycle
            if s_ack:
                s_ack = 0
            elif s_req:
                if s_wait == 0:
                    s_req, s_ack = 0, 1
                else:
                    s_wait -= 1
            elif rnd.random() < 0.1:
                s_req, s_wait = 1, rnd.randint(0, 2)
            s_level = rnd.randrange(15)
    return rows


class _Group:
    def __init__(self):
        self.failures = []

    def _report_failure(self, h, count, starts, at_end):
        self.failures.append(h.name)


class _Binder:
    def __init__(self, dom, row):
        self.dom, self.row = dom, row

    def lookup(self, path):
        name = ".".join(path)
        if name not in WIDTHS:
            return None
        row = self.row
        slot = self.dom.value_slot(name, lambda: row[name], name)
        return SignalRef(slot, WIDTHS[name], False, label=name)

    def suggest(self, path):
        return []

    def x_slot(self, ref):
        raise NotImplementedError

    def history_channel(self, key, code, width, gcode, depth):
        return self.dom.channel(key, code, width, gcode, depth)


def build(sets, label):
    dom = _Domain(label, None, "posedge", label)
    dom.idle_sleep = True
    row = {k: 0 for k in WIDTHS}
    binder = _Binder(dom, row)
    group = _Group()
    for kind, texts in sets:
        for i, text in enumerate(texts):
            spec = parse_assertion(f"disable iff (!rst_n) {text}" if kind == "assert" else text)
            prog = Compiler(binder, text, {}).compile(spec, kind)
            h = AssertionHandle(group, text, prog, dom, "error", None, None, None)
            dom.monitors.append(h)
            dom.steps.append(h.step)
    dom._run = None
    return dom, row, group


def run(fast=True, local=True, covers=True, slow=True, rows=None):
    sets = [("assert", FAST if fast else []), ("assert", LOCAL if local else []),
            ("cover", COVERS if covers else [])]
    d1, r1, g1 = build(sets, "main")
    d2, r2, g2 = build([("assert", SLOW if slow else [])], "slow")
    doms = [(d1, r1), (d2, r2)] if slow else [(d1, r1)]
    t0 = time.perf_counter()
    for cyc, r in enumerate(rows):
        for k, (d, row) in enumerate(doms):
            if k == 1 and cyc % 2:
                continue
            row.update(r)
            if d._tick() and not d._busy:
                d._maybe_sleep()
    dt = time.perf_counter() - t0
    n = sum(len(d.monitors) for d, _ in doms)
    return dt, n, g1.failures + g2.failures


def bare(rows):
    row = {}
    t0 = time.perf_counter()
    for r in rows:
        row.update(r)
    return time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=20000)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()
    _api._sim_steps = lambda: 0
    rows = stimulus(args.cycles)
    if args.profile:
        import cProfile
        import pstats
        cProfile.runctx("run(rows=rows)", globals(), locals(), "real.prof")
        pstats.Stats("real.prof").sort_stats("tottime").print_stats(20)
        Path("real.prof").unlink()
        return
    base = min(bare(rows) for _ in range(args.repeat))
    variants = [("all", {}), ("fast only", dict(local=False, covers=False, slow=False)),
                ("local only", dict(fast=False, covers=False, slow=False)),
                ("covers only", dict(fast=False, local=False, slow=False)),
                ("slow domain only", dict(fast=False, local=False, covers=False)),
                ("no local", dict(local=False))]
    modes = [("python", None)] + ([("kernel", _engine._kmod)] if _engine._kmod else [])
    print(f"{'variant':18} {'mode':7} {'n':>3} {'us/cycle':>9}  failures")
    for name, kw in variants:
        for mode, kmod in modes:
            saved = _engine._kmod
            _engine._kmod = kmod
            try:
                res = [run(rows=rows, **kw) for _ in range(args.repeat)]
            finally:
                _engine._kmod = saved
            dt, n, fails = min(res, key=lambda x: x[0])
            us = (dt - base) * 1e6 / len(rows)
            print(f"{name:18} {mode:7} {n:3} {us:9.2f}  {len(fails)}"
                  + (f" ({sorted(set(fails))[:3]})" if fails else ""))


if __name__ == "__main__":
    main()
