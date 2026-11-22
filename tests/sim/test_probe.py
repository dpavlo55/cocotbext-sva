"""Assertions evaluated on the samples of generated HDL probes must give the
results of the same assertions sampled from Python at the clock edge:
failures with their times and start times, and counters. One probe hands
over every sample (depth 1), the other one buffers 16."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

import probes
from cocotbext.sva import Assertions, posedge
from test_offline_sim import PROPS
from test_sim import INPUTS, cycle

EXTRA = {
    "unknown": ("assert", "!$isunknown(count)"),
    "sliced": ("assert", "rsp |-> rtag[3:2] == $past(tag[3:2], 3) || !$past(rst_n, 3)"),
}


def norm(errors):
    return sorted((f.name.split(".", 1)[1], f.time, tuple(f.start_times), f.count, f.at_end)
                  for f in errors)


def strip(stats):
    return {k.split(".", 1)[1]: v for k, v in stats.items()}


@cocotb.test()
async def test_probe_matches_edge_sampling(dut):
    core = dut.u_dut if probes.VHDL else dut
    for n in INPUTS:
        getattr(dut, n).value = 0
    dut.tag.value = 0
    dut.rst_n.value = 0
    Clock(dut.clk, 10, unit="ns").start()
    await FallingEdge(dut.clk)
    pr = probes.make()
    groups = {
        "edge": Assertions(core, clock=posedge(dut.clk), disable_iff="!rst_n", name="edge"),
        "p1": Assertions(core, clock=pr[1].attach(dut), disable_iff="!rst_n", name="p1"),
        "p16": Assertions(core, clock=pr[16].attach(dut), disable_iff="!rst_n", name="p16"),
    }
    for g in groups.values():
        for name, (kind, text) in {**PROPS, **EXTRA}.items():
            (g.cover_property if kind == "cover" else g.assert_property)(text, name=name)
    rnd = random.Random(12)
    mid = None
    for i in range(403):
        await cycle(dut, req=int(rnd.random() < 0.35), drop=int(rnd.random() < 0.15),
                    en=int(rnd.random() < 0.5), start=int(rnd.random() < 0.15),
                    tag=rnd.randint(0, 15), corrupt=int(rnd.random() < 0.1))
        if i in (3, 200):
            dut.rst_n.value = 1 if i == 3 else 0
        if i == 204:
            dut.rst_n.value = 1
        if i == 150:
            # read in the middle of a buffer: after the falling edge, when the
            # probes have stored the sample of this cycle
            await FallingEdge(dut.clk)
            mid = {k: strip(g.stats()) for k, g in groups.items()}
    await FallingEdge(dut.clk)
    res = {k: (g.finalize(raise_on_failure=False), strip(g.stats())) for k, g in groups.items()}
    assert len(res["edge"][0]) > 20, len(res["edge"][0])
    assert any(f.at_end for f in res["edge"][0])
    for k in ("p1", "p16"):
        assert mid[k] == mid["edge"], k
        assert norm(res[k][0]) == norm(res["edge"][0]), k
        assert res[k][1] == res["edge"][1], k
        dom = groups[k].assertions[0].domain
        assert dom.overruns == 0
        assert dom.tickno == groups["edge"].assertions[0].domain.tickno


@cocotb.test()
async def test_probe_errors(dut):
    from cocotbext.sva import SvaError, SvaNameError
    core = dut.u_dut if probes.VHDL else dut
    Clock(dut.clk, 10, unit="ns").start()
    await FallingEdge(dut.clk)
    a = Assertions(core, clock=probes.make()[1].attach(dut), name="err")
    for text, kw, exc, msg in (
            ("req |-> drop", {}, SvaNameError, "unknown name 'drop'"),
            ("req |-> x", {"values": {"x": lambda: 1}}, SvaError, "values= cannot be used with a probe"),
            ("req |=> @(posedge clk) ack", {}, SvaError, "one clock per probe")):
        try:
            a.assert_property(text, **kw)
        except exc as e:
            assert msg in str(e), e
        else:
            raise AssertionError(f"no error for {text}")
    a.finalize()
