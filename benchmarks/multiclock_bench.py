"""Multi-clock and local-variable assertions without a simulator: compiled
kernel against pure Python, through the trace runner (the per-tick work of a
multi-clock domain: rewritten properties, attempts gated on the leading
clock).

Two clocks: c0 ticks in every time step, c1 in two of three. req on ~25 % of
the steps, ack follows req at the c1 ticks; `rare` on ~1/256 of the steps.

    python benchmarks/multiclock_bench.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocotbext.sva import _engine, _trace  # noqa: E402

SCENARIOS = {
    "busy": "@(posedge c0) req |=> @(posedge c1) ##[1:3] ack",
    "idle": "@(posedge c0) rare |=> @(posedge c1) ##[0:2] !rare",
    "local": "@(posedge c0) (req, v = s8) |=> @(posedge c1) ##[1:3] (ack && s8 != v + 8'd1)",
}
SIGNALS = {"c0": 1, "c1": 1, "req": 1, "ack": 1, "rare": 1, "s8": 8}


def rows(n):
    lfsr = 0x1234567
    out = []
    req = ack = 0
    for k in range(n):
        c1 = int(k % 3 != 1)
        out.append({"c0": 1, "c1": c1, "req": req, "ack": ack, "rare": int(lfsr & 0xFF == 0x5A),
                    "s8": (lfsr >> 8) & 0xFF})
        req = (lfsr >> 3) & (lfsr >> 7) & 1
        ack = req if c1 else ack
        lfsr = (lfsr >> 1) ^ 0x80200003 if lfsr & 1 else lfsr >> 1
    return out


def run(text, n, rs, kernel):
    saved = _engine._kmod
    if not kernel:
        _engine._kmod = None
    try:
        tr = _trace.TraceRun({f"p{i}": text for i in range(n)}, SIGNALS)
        t0 = time.perf_counter()
        for r in rs:
            tr.step(r)
        dt = time.perf_counter() - t0
        in_kernel = sum(m._k is not None for m in tr.mons)
        return dt, tr.finish().stats["p0"], in_kernel
    finally:
        _engine._kmod = saved


def main():
    _trace._Sink.wants_pass = _trace._Sink.wants_vacuous = False    # report failures only
    rs = rows(20000)
    modes = [False] + ([True] if _engine._kmod is not None else [])
    print(f"{'scenario':8} {'n':>4} " + " ".join(f"{'kernel' if k else 'python':>16}" for k in modes)
          + "   (us per tick)")
    for name, text in SCENARIOS.items():
        for n in (10, 50):
            cols = []
            for kernel in modes:
                best = min(run(text, n, rs, kernel)[0] for _ in range(3))
                cols.append(f"{best * 1e6 / len(rs):16.2f}")
            print(f"{name:8} {n:4} " + " ".join(cols))


if __name__ == "__main__":
    main()
