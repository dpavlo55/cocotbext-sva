"""Online half of the offline check of clocks not driven from cocotb and of
multi-clock properties (driven by run_offline.py, design `clocks`): the
assertions run online while the simulator writes a dump; the failures and
counters are saved to $OFFLINE_RESULT for comparison with TraceSampler on the
dump. Online sampling is preponed here (the clocks are derived or generated
in the HDL), which is what offline checking does."""
import json
import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.simtime import get_sim_time
from cocotb.triggers import FallingEdge, RisingEdge

from cocotbext.sva import Assertions, posedge

# name -> (kind, property); the default clock is posedge clk
PROPS = {
    # one clock, derived (clk_div) or generated (clk_int) in the HDL
    "div_cap": ("assert", "@(posedge clk_div) cnt > 8'd3 |-> cap == cnt - 8'd1"),
    "div_seen": ("assert", "@(posedge clk_div) req_qq |=> seen2"),
    "div_wrong": ("assert", "@(posedge clk_div) req_q == seen"),                   # fails
    "int_count": ("assert", "@(posedge clk_int) cnt_i != 8'd0 |=> cnt_i == $past(cnt_i) + 8'd1"),
    "int_neg": ("assert", "@(negedge clk_int) valid_b |-> data_b == data_a"),
    # several clocks
    "arrives": ("assert", "send |=> @(posedge clk_int) ##3 valid_b"),
    "data": ("assert", "(send, v = din) |=> @(posedge clk_int) ##3 (valid_b && data_b == v)"),
    "early": ("assert", "send |=> @(posedge clk_int) ##2 valid_b"),               # fails
    "back": ("assert", "@(posedge clk_int) $rose(valid_b) |-> @(posedge clk) $stable(data_a)"),
    "same": ("assert", "req && !clk_div |-> @(posedge clk_div) seen"),           # fails
    "next": ("assert", "req && !clk_div |=> @(posedge clk_div) seen"),
    "cov": ("cover", "send ##1 @(posedge clk_int) 1 ##3 valid_b ##1 !valid_b"),
    "cov_both": ("cover", "@(posedge clk_div) 1 ##0 @(posedge clk) 1"),
    "never": ("assert", "send |=> @(posedge clk_int) s_eventually (valid_b && !valid_b)"),   # pending
}
CLOCK = "posedge clk"


@cocotb.test()
async def test_online_with_dump(dut):
    dut.req.value = 0
    dut.send.value = 0
    dut.din.value = 0
    # created before the cocotb clock starts (clk_int first rises at 7 ns)
    a = Assertions(dut, clock=posedge(dut.clk, sampling="preponed"), name="o")
    for name, (kind, text) in PROPS.items():
        (a.cover_property if kind == "cover" else a.assert_property)(text, name=name)
    Clock(dut.clk, 10, unit="ns").start(start_high=False)
    rnd = random.Random(3)
    gap = 0
    for k in range(300):
        await FallingEdge(dut.clk)
        dut.req.value = int(rnd.random() < 0.5)
        send = int(gap > 6 and rnd.random() < 0.3 and k < 290)     # one word in flight at a time
        gap = 0 if send else gap + 1
        dut.send.value = send
        dut.din.value = rnd.randint(0, 255)
        await RisingEdge(dut.clk)
    dut.req.value = 0
    dut.send.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
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
