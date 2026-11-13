"""End-to-end tests of cocotbext.sva on a real simulator.

Inputs are driven on the falling edge; `cycle()` returns the time of the rising
edge that samples them. Expected failure times are derived from those times.
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.simtime import get_sim_time
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from cocotbext.sva import Assertions, Checker, Property, checked, posedge

PERIOD = 10
STATES = {"S_IDLE": 0, "S_BUSY": 1, "S_DONE": 2}
INPUTS = ("req", "drop", "start", "en", "corrupt")


async def setup(dut):
    Clock(dut.clk, PERIOD, unit="ns").start()
    for n in INPUTS:
        getattr(dut, n).value = 0
    dut.tag.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await cycle(dut)
    dut.rst_n.value = 1


async def cycle(dut, **vals):
    """Drive `vals` (others back to 0) on the falling edge; return the time of the
    rising edge that samples them, in ns."""
    await FallingEdge(dut.clk)
    for n in INPUTS:
        getattr(dut, n).value = vals.get(n, 0)
    if "tag" in vals:
        dut.tag.value = vals["tag"]
    await RisingEdge(dut.clk)
    return get_sim_time("ns")


def group(dut, **kw):
    return Assertions(dut, clock=posedge(dut.clk), disable_iff="!rst_n", name="t",
                      enums=STATES, **kw)


class HandshakeChecker(Checker):
    ports = ("clk", "rst_n", "req", "ack")
    clock = "posedge clk"
    disable_iff = "!rst_n"

    def build(self, a):
        a.assert_property("req |-> ##[1:3] ack", name="answered")
        a.cover_property("req ##1 req", name="back_to_back")


EXACT_LATENCY = Property("r |=> ##(N - 1) a", args=("r", "a", "N")) if False else \
    Property("r |=> ##1 a", args=("r", "a"))


@cocotb.test()
@checked
async def test_clean_traffic_passes(dut):
    await setup(dut)
    a = group(dut)
    rq = a.assert_property("req |-> ##[1:3] ack", name="req_gets_ack")
    a.assert_property(EXACT_LATENCY(r="req", a=dut.ack), name="exact_latency")
    fsm = a.assert_property("start && state == S_IDLE |=> busy[*3] ##1 done ##1 !busy", name="fsm")
    a.assert_property("en |=> count == $past(count) + 8'd1", name="count_inc")
    a.assert_property("!en |=> $stable(count)", name="count_hold")
    tagp = a.assert_property("(req, t = tag) |-> ##3 (rsp && rtag == t)", name="tag_returns")
    a.assert_property("busy |-> !done", name="busy_not_done")
    a.assert_property("count <= limit", name="py_value", values={"limit": lambda: 255})
    cov = a.cover_property("req ##1 req ##1 req", name="three_reqs")
    hs = HandshakeChecker(a, name="hs", clk=dut.clk, rst_n=dut.rst_n, req=dut.req, ack=dut.ack)
    a.check("count == 0", msg="counter not cleared by reset")
    rnd = random.Random(1)
    for i in range(300):
        await cycle(dut, req=int(rnd.random() < 0.4), en=int(rnd.random() < 0.5),
                    start=int(rnd.random() < 0.1), tag=rnd.randint(0, 15))
        a.check("!(busy && done)", deferred=True)
    for _ in range(6):
        await cycle(dut)
    st = a.stats()
    assert st["t.req_gets_ack"]["passes"] > 50, st["t.req_gets_ack"]
    assert st["t.tag_returns"]["passes"] > 50
    assert st["t.fsm"]["passes"] > 3
    assert st["t.three_reqs"]["hits"] > 3
    assert st["t.hs.answered"]["passes"] == st["t.req_gets_ack"]["passes"]
    assert not a.errors


async def _run_scenario(dut, steps):
    """Drive a list of dicts; return the sample time of each step."""
    return [await cycle(dut, **s) for s in steps]


@cocotb.test()
async def test_dropped_ack_reported_at_exact_time(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("req |-> ##[1:3] ack", name="window")
    a.assert_property("req |=> ##1 ack", name="exact")
    steps = [{}, {"req": 1}, {}, {}, {}, {"req": 1, "drop": 1}, {}, {}, {}, {}, {}]
    times = await _run_scenario(dut, steps)
    errors = a.finalize(raise_on_failure=False)
    t_req = times[5]
    by_name = {f.name: f for f in errors}
    assert set(by_name) == {"t.window", "t.exact"}, [str(f) for f in errors]
    assert by_name["t.window"].time == t_req + 3 * PERIOD
    assert by_name["t.window"].start_times == [t_req]
    assert by_name["t.exact"].time == t_req + 2 * PERIOD
    assert by_name["t.window"].values["req"] == 0 and by_name["t.window"].values["ack"] == 0


@cocotb.test()
async def test_local_variable_detects_corrupted_tag(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("(req, t = tag) |-> ##3 (rsp && rtag == t)", name="tag")
    steps = [{"req": 1, "tag": 3}, {"req": 1, "tag": 9, "corrupt": 1}, {"req": 1, "tag": 4}, {}, {}, {}, {}]
    times = await _run_scenario(dut, steps)
    errors = a.finalize(raise_on_failure=False)
    assert [(f.time, f.start_times) for f in errors] == [(times[1] + 3 * PERIOD, [times[1]])]
    assert a.stats()["t.tag"]["passes"] == 2


@cocotb.test()
async def test_disable_iff_and_assertion_control(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("req |-> ##[1:3] ack", name="window")
    await cycle(dut, req=1, drop=1)
    dut.rst_n.value = 0                       # reset while the attempt is in flight
    await cycle(dut)
    dut.rst_n.value = 1
    for _ in range(4):
        await cycle(dut)
    a.off("window")                           # no new attempts while off
    await cycle(dut, req=1, drop=1)
    a.on("window")
    for _ in range(5):
        await cycle(dut)
    errors = a.finalize(raise_on_failure=False)
    assert errors == [], [str(f) for f in errors]
    assert a.stats()["t.window"]["disabled"] >= 1


@cocotb.test()
async def test_strong_obligation_fails_at_end(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("start |-> strong(##[1:$] count == 8'hFF)", name="never_reached")
    a.assert_property("start |-> ##[1:$] count == 8'hFF", name="weak_ok")
    await cycle(dut, start=1)
    for _ in range(5):
        await cycle(dut)
    errors = a.finalize(raise_on_failure=False)
    assert [(f.name, f.at_end) for f in errors] == [("t.never_reached", True)]


@cocotb.test()
async def test_immediate_assertions(dut):
    await setup(dut)
    a = group(dut)
    t = get_sim_time("ns")
    assert a.check("count == 1", msg="expected one") is False      # fails, test continues
    assert a.check(lambda: True) is True
    a.cover("count == 0", name="zero")
    await cycle(dut, en=1)
    await Timer(1, unit="ns")
    # deferred: only the last call in the time step counts
    a.check("count == 0", name="deferred_last", deferred=True)
    a.check("count == 1", name="deferred_last", deferred=True)
    await Timer(1, unit="ns")
    errors = a.finalize(raise_on_failure=False)
    assert [(f.time, f.message, f.immediate) for f in errors] == [(t, "expected one", True)]
    st = a.stats()
    assert st["t.zero"]["hits"] == 1 and st["t.deferred_last"]["failures"] == 0


@cocotb.test(expect_fail=True)
@checked
async def test_checked_fails_the_test_at_the_end(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("req |-> ##[1:3] ack", name="window")
    await cycle(dut, req=1, drop=1)
    for _ in range(5):
        await cycle(dut)
    # reaching here proves the failure did not stop the test; @checked fails it now


@cocotb.test(expect_fail=True)
async def test_fatal_severity_stops_the_test(dut):
    await setup(dut)
    a = group(dut)
    a.assert_property("req |-> ##[1:3] ack", name="window", severity="fatal")
    await cycle(dut, req=1, drop=1)
    for _ in range(10):
        await cycle(dut)
    raise RuntimeError("not reached: the fatal failure should have ended the test")


@cocotb.test()
async def test_async_with_limits_checking(dut):
    await setup(dut)
    try:
        async with group(dut) as a:
            a.assert_property("req |-> ##[1:3] ack", name="window")
            await cycle(dut, req=1, drop=1)
            for _ in range(5):
                await cycle(dut)
    except AssertionError as e:
        assert "t.window" in str(e)
    else:
        raise AssertionError("async with did not raise for the failure")
