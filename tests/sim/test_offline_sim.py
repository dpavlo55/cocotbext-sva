"""Online half of the offline check (driven by run_offline.py): assertions run
online while the simulator writes a VCD dump; the failures and counters are
saved to $OFFLINE_RESULT for comparison with TraceSampler on the dump."""
import json
import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.simtime import get_sim_time
from cocotb.triggers import FallingEdge

from cocotbext.sva import Assertions, posedge
from test_sim import INPUTS, cycle

# name -> (kind, property); no enum-typed signal (dumps write VHDL enums as text)
PROPS = {
    "window": ("assert", "req |-> ##[1:3] ack"),
    "exact": ("assert", "req |-> ##2 ack"),
    "rose": ("assert", "$rose(req) |=> ##1 ack"),
    "past": ("assert", "en |=> count == $past(count) + 8'd1"),
    "past2": ("assert", "$past(en, 2) && !$past(en) |-> count != 8'd0"),
    "tag": ("assert", "(req, t = tag) |-> ##3 (rsp && rtag == t)"),
    "fsm": ("assert", "start && !busy && !done |=> busy[*3] ##1 done ##1 !busy"),
    "stable": ("assert", "busy |=> $stable(count) || en"),
    "strong": ("assert", "start |-> s_eventually [1:6] done"),
    "never": ("assert", "start |-> strong(##[1:$] count == 8'hFF)"),
    "cov": ("cover", "req ##1 req ##1 !req"),
    "cov_fsm": ("cover", "start ##[1:4] done"),
}
CLOCK = "posedge clk"
DISABLE = "!rst_n"


@cocotb.test()
async def test_online_with_dump(dut):
    for n in INPUTS:
        getattr(dut, n).value = 0
    dut.tag.value = 0
    dut.rst_n.value = 0
    # created before the clock starts: online sees every edge of the dump
    a = Assertions(dut, clock=posedge(dut.clk), disable_iff=DISABLE, name="o")
    for name, (kind, text) in PROPS.items():
        (a.cover_property if kind == "cover" else a.assert_property)(text, name=name)
    Clock(dut.clk, 10, unit="ns").start(start_high=False)
    for _ in range(3):
        await cycle(dut)
    dut.rst_n.value = 1
    rnd = random.Random(11)
    for k in range(400):
        if k == 200:
            dut.rst_n.value = 0
        if k == 202:
            dut.rst_n.value = 1
        await cycle(dut, req=int(rnd.random() < 0.4), drop=int(rnd.random() < 0.05),
                    en=int(rnd.random() < 0.5), start=int(rnd.random() < 0.1),
                    tag=rnd.randint(0, 15), corrupt=int(rnd.random() < 0.03))
    # half a cycle more: simulators may not write the last time step of a dump
    await FallingEdge(dut.clk)
    errors = a.finalize(raise_on_failure=False)
    out = {
        "failures": sorted([f.name.split(".", 1)[1], f.time, list(f.start_times), f.count, f.at_end]
                           for f in errors),
        "stats": {k.split(".", 1)[1]: v for k, v in a.stats().items()},
        "end": get_sim_time("ns"),
    }
    with open(os.environ["OFFLINE_RESULT"], "w") as fh:
        json.dump(out, fh)
