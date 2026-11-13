"""Sampling calibration: does a read at a RisingEdge callback return the
pre-edge (SVA-correct) or the post-edge value?

For each probe we read the signal at the edge callback and again in ReadOnly of
the same time step (settled, post-edge). edge == settled - 1  -> "pre";
edge == settled -> "post". Results are appended as JSON lines to CALIB_OUT.
"""
import json
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

SAMPLES = 8
SIM = os.environ.get("CALIB_SIM", "?")
LANG = os.environ.get("CALIB_LANG", "?")


def _classify(pairs):
    kinds = set()
    for at_edge, settled in pairs:
        if at_edge == settled - 1:
            kinds.add("pre")
        elif at_edge == settled:
            kinds.add("post")
        else:
            kinds.add(f"odd({at_edge},{settled})")
    return kinds.pop() if len(kinds) == 1 else "mixed:" + "/".join(sorted(kinds))


def _record(case, results):
    line = {"sim": SIM, "lang": LANG, "case": case, **results}
    cocotb.log.info("CALIB %s", json.dumps(line))
    out = os.environ.get("CALIB_OUT")
    if out:
        with open(out, "a") as f:
            f.write(json.dumps(line) + "\n")


async def _measure(clock_sig, probes):
    pairs = {name: [] for name in probes}
    edge = RisingEdge(clock_sig)
    await edge  # skip the first edge (initialisation effects)
    for _ in range(SAMPLES):
        await edge
        at_edge = {n: int(h.value) for n, h in probes.items()}
        await ReadOnly()
        for n, h in probes.items():
            pairs[n].append((at_edge[n], int(h.value)))
    return {n: _classify(p) for n, p in pairs.items()}


async def _ext_clock_case(dut, impl):
    clock = Clock(dut.clk, 10, unit="ns", impl=impl)
    clock.start()
    res = await _measure(dut.clk, {"cnt_ext": dut.cnt_ext, "cnt_comb": dut.cnt_comb})
    clock.stop()
    await Timer(20, unit="ns")
    return res


@cocotb.test()
async def ext_clock_gpi(dut):
    """Clock driven from cocotb with the C-level (GPI) clock."""
    _record("cocotb clock (gpi)", await _ext_clock_case(dut, "gpi"))


@cocotb.test()
async def ext_clock_py(dut):
    """Clock driven from cocotb with the Python clock coroutine."""
    _record("cocotb clock (py)", await _ext_clock_case(dut, "py"))


@cocotb.test()
async def int_clock(dut):
    """Clock generated inside the HDL."""
    _record("HDL-generated clock", await _measure(dut.clk_int, {"cnt_int": dut.cnt_int}))


@cocotb.test()
async def div_clock(dut):
    """Clock derived inside the HDL from the cocotb clock (divide by 2).

    cnt_div is in the derived domain; cnt_ext is in the source domain and also
    changes at the same time step (SVA samples it pre-edge).
    """
    clock = Clock(dut.clk, 10, unit="ns")
    clock.start()
    res = await _measure(dut.clk_div, {"cnt_div": dut.cnt_div, "cnt_ext(src)": dut.cnt_ext})
    clock.stop()
    _record("HDL-derived clock", res)
