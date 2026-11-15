"""Engine benchmark without a simulator (phase 3).

Drives the real clocking-domain tick loop (`_api._Domain._tick` and the idle
sleep check, the code the clock callback runs) with the stimulus of the simulator benchmark
(benchmarks/bench.sv: req on ~25 % of cycles, ack two cycles later; `rare` on
~1/256 of cycles). Signal reads are plain Python functions, so the result is
the engine's own cost; a real GPI read adds ~0.1-0.3 us per distinct signal
per cycle on top.

    python benchmarks/engine_bench.py                  # all scenarios
    python benchmarks/engine_bench.py same idle -n 50  # chosen scenarios
    python benchmarks/engine_bench.py same --profile   # cProfile of one run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocotbext.sva import _api  # noqa: E402
from cocotbext.sva._api import AssertionHandle, _Domain  # noqa: E402
from cocotbext.sva._compile import Compiler  # noqa: E402
from cocotbext.sva._expr import SignalRef  # noqa: E402
from cocotbext.sva._parser import parse_assertion  # noqa: E402

SCENARIOS = {
    "same": ["req |-> ##[1:3] ack"],
    "mixed": ["req |-> ##[1:3] ack", "req |=> ##1 ack", "ack |-> $past(req, 2)",
              "$rose(req) |-> ##2 ack", "req ##1 req |-> ##1 ack ##1 ack"],
    "idle": ["rare |-> ##[1:3] !rare", "rare |=> !rare"],
    "local": ["(req, v = s8) |-> ##2 ack && v < 9'd256"],
    # 50 different properties that all hold (ack follows req after 2 cycles)
    "distinct": ([f"req |-> ##[{a}:{b}] ack" for a in range(3) for b in range(2, 7)]
                 + [f"req |=> ##[{a}:{b}] ack" for a in range(2) for b in range(1, 6)]
                 + [f"req && s8[{k}] |-> ##2 ack" for k in range(8)]
                 + [f"(req ##1 1) |-> ##[0:{k}] ack" for k in range(1, 6)]
                 + [f"$rose(req) |-> ##2 ack ##[0:{k}] 1" for k in range(6)]
                 + [f"req |-> ##2 ack[*1:{k}]" for k in range(1, 7)]),
    # 50 different rarely triggered properties
    "idle_distinct": [f"rare && s8[{k % 8}:{k % 8}] == {k // 8 % 2} |=> ##[0:{k % 5}] !rare"
                      for k in range(50)],
}


def stimulus(cycles: int):
    """Rows {signal: value} as sampled at each rising edge (pre-edge values)."""
    lfsr = [(0x1234567 + i * 7919 + 1) & 0xFFFFFFFF for i in range(10)]
    req = req_d1 = ack = 0
    rows = []
    for _ in range(cycles):
        rows.append({"req": req, "ack": ack, "rare": int((lfsr[9] & 0xFF) == 0x5A),
                     "s8": lfsr[0] & 0xFF})
        n_req = (lfsr[0] >> 3) & (lfsr[0] >> 7) & 1
        ack, req_d1, req = req_d1, req, n_req
        lfsr = [(x >> 1) ^ 0x80200003 if x & 1 else x >> 1 for x in lfsr]
    return rows


class _Group:
    """Just enough of Assertions for AssertionHandle's sink."""
    def __init__(self):
        self.failures = 0

    def _report_failure(self, *a, **k):
        self.failures += 1


class _Binder:
    WIDTH = {"req": 1, "ack": 1, "rare": 1, "s8": 8}

    def __init__(self, dom, row):
        self.dom, self.row = dom, row

    def lookup(self, path):
        name = ".".join(path)
        if name not in self.WIDTH:
            return None
        row = self.row
        slot = self.dom.value_slot(name, lambda: row[name], name)
        return SignalRef(slot, self.WIDTH[name], False, label=name)

    def suggest(self, path):
        return []

    def x_slot(self, ref):
        raise NotImplementedError

    def history_channel(self, key, code, width, gcode, depth):
        return self.dom.channel(key, code, width, gcode, depth)


def build(texts, n):
    dom = _Domain("bench", None, "posedge", "bench")
    # the sleep check runs as in the simulator; the domain never actually
    # sleeps here (signals are Python values, which cannot be watched)
    dom.idle_sleep = True
    row = {"req": 0, "ack": 0, "rare": 0, "s8": 0}
    binder = _Binder(dom, row)
    group = _Group()
    for i in range(n):
        text = texts[i % len(texts)]
        prog = Compiler(binder, text, {}).compile(parse_assertion(text), "assert")
        h = AssertionHandle(group, f"p{i}", prog, dom, "error", None, None, None)
        dom.monitors.append(h)
        dom.steps.append(h.step)
    dom._run = None
    return dom, row, group


def run(texts, n, rows):
    dom, row, group = build(texts, n)
    tick, maybe_sleep = dom._tick, dom._maybe_sleep
    t0 = time.perf_counter()
    for r in rows:
        row.update(r)
        if tick() and not dom._busy:     # as in _Domain._on_clock
            maybe_sleep()
    dt = time.perf_counter() - t0
    st = [h.monitor.stats.as_dict() for h in dom.monitors]
    assert group.failures == 0 or "idle" in str(texts), group.failures
    return dt, st, group.failures


def bare(rows):
    row = {}
    t0 = time.perf_counter()
    for r in rows:
        row.update(r)
    return time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    ap.add_argument("-n", type=int, nargs="*", default=[1, 10, 50])
    ap.add_argument("--cycles", type=int, default=20000)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()
    _api._sim_steps = lambda: 0          # no simulator: start times are not needed
    rows = stimulus(args.cycles)
    base = min(bare(rows) for _ in range(args.repeat))
    if args.profile:
        import cProfile
        import pstats
        sc, n = args.scenarios[0], max(args.n)
        cProfile.runctx("run(SCENARIOS[sc], n, rows)", globals(), locals(), "engine.prof")
        pstats.Stats("engine.prof").sort_stats("tottime").print_stats(15)
        Path("engine.prof").unlink()
        return
    print(f"{'scenario':8} {'n':>4} {'us/cycle':>9} {'us/assertion/cycle':>19}")
    for sc in args.scenarios:
        for n in args.n:
            best = min(run(SCENARIOS[sc], n, rows)[0] for _ in range(args.repeat)) - base
            per_cycle = best * 1e6 / len(rows)
            print(f"{sc:8} {n:4} {per_cycle:9.2f} {per_cycle / n:19.3f}")


if __name__ == "__main__":
    main()
