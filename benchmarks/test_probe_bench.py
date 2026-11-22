"""Speed of assertions sampled by a generated HDL probe (see run_probe.py).

Scenarios, each over BENCH_CYCLES clock cycles with no other Python activity:
the baseline without Python, then 50 assertions sampled at the clock edge,
through a probe of depth 1 and through a probe of depth 64."""
import json
import os
import time

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from cocotbext.sva import Assertions, Probe, posedge

SIM = os.environ.get("CALIB_SIM", "?")
LANG = os.environ.get("CALIB_LANG", "?")
CYCLES = int(os.environ.get("BENCH_CYCLES", "20000"))
VHDL = LANG == "vhdl"
SIGNALS = {"req": 1, "ack": 1, "rare": 1, "s8": 8}
TOP = dict(top="bench_probe", scope="u") if VHDL else dict(top="bench")
SAME = ["req |-> ##[1:3] ack"]
MIXED = ["req |-> ##[1:3] ack", "req |=> ##1 ack", "ack |-> $past(req, 2)",
         "$rose(req) |-> ##2 ack", "req ##1 req |-> ##1 ack ##1 ack"]
IDLE = ["rare |-> ##[1:3] !rare", "rare |=> !rare"]


def _record(**data):
    line = {"sim": SIM, "lang": LANG, "kind": "speed", **data}
    cocotb.log.info("BENCH %s", json.dumps(line))
    out = os.environ.get("BENCH_OUT")
    if out:
        with open(out, "a") as f:
            f.write(json.dumps(line) + "\n")


async def _run(dut, scenario, texts=None, depth=None, k=50):
    clock = Clock(dut.clk, 10, unit="ns", impl="gpi")
    clock.start()
    await RisingEdge(dut.clk)
    core = dut.u if VHDL else dut
    stats = {}
    t0 = time.perf_counter()
    if texts is None:
        await Timer(CYCLES * 10, unit="ns")
    else:
        if depth is None:
            clk = posedge(dut.clk)
        else:
            clk = Probe(f"b{depth}", "posedge clk", SIGNALS, depth=depth, **TOP).attach(dut)
        a = Assertions(core, clock=clk, name="b")
        for i in range(k):
            a.assert_property(texts[i % len(texts)], name=f"p{i}")
        await Timer(CYCLES * 10, unit="ns")
        errs = a.finalize(raise_on_failure=False)
        st = a.stats()
        stats = {"passes": sum(v.get("passes", 0) for v in st.values()), "failures": len(errs),
                 "attempts": st["b.p0"]["attempts"]}
    wall = time.perf_counter() - t0
    clock.stop()
    _record(scenario=scenario, cycles=CYCLES, wall_s=round(wall, 4),
            cycles_per_s=round(CYCLES / wall), **stats)
    await Timer(20, unit="ns")


@cocotb.test()
async def speed_0_no_python(dut):
    await _run(dut, "0 no Python per cycle")


@cocotb.test()
async def speed_same_edge(dut):
    await _run(dut, "x50 same, edge", SAME)


@cocotb.test()
async def speed_same_probe_1(dut):
    await _run(dut, "x50 same, probe depth 1", SAME, depth=1)


@cocotb.test()
async def speed_same_probe_64(dut):
    await _run(dut, "x50 same, probe depth 64", SAME, depth=64)


@cocotb.test()
async def speed_mixed_edge(dut):
    await _run(dut, "x50 mixed, edge", MIXED)


@cocotb.test()
async def speed_mixed_probe_64(dut):
    await _run(dut, "x50 mixed, probe depth 64", MIXED, depth=64)


@cocotb.test()
async def speed_idle_edge(dut):
    await _run(dut, "x50 idle, edge (idle sleep)", IDLE)


@cocotb.test()
async def speed_idle_probe_64(dut):
    await _run(dut, "x50 idle, probe depth 64", IDLE, depth=64)
