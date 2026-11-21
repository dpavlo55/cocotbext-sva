"""End-to-end tests with clocks that are not driven from cocotb.

`clocks` has a clock derived from the cocotb clock (clk_div) and a clock
generated inside the HDL (clk_int). The properties below hold with SVA
sampling (values from before the time step of the clock edge) and fail when
values are read at the clock edge after the source domain has updated.
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.simtime import get_sim_time
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from cocotbext.sva import Assertions, posedge

PERIOD = 10
# Hold with preponed sampling at posedge clk_div:
#   cap and seen were taken from the clk domain *after* its update at the
#   previous clk_div edge; cnt and req_qq are sampled before this one.
DIV_PROPS = {
    "cap": "cnt > 8'd3 |-> cap == cnt - 8'd1",
    "seen_eq": "req_qq == seen",
    "seen_imp": "req_qq |-> seen",
    "seen_next": "req_qq |=> seen2",
}


async def setup(dut):
    dut.req.value = 0
    dut.send.value = 0
    dut.din.value = 0
    Clock(dut.clk, PERIOD, unit="ns").start()
    await RisingEdge(dut.clk)


async def cycle(dut, **vals):
    await FallingEdge(dut.clk)
    dut.req.value = vals.get("req", 0)
    dut.send.value = vals.get("send", 0)
    if "din" in vals:
        dut.din.value = vals["din"]
    await RisingEdge(dut.clk)


async def traffic(dut, n, seed=1, p=0.5):
    rnd = random.Random(seed)
    for _ in range(n):
        await cycle(dut, req=int(rnd.random() < p))


def add(a, props):
    return {n: (a.cover_property if n.startswith("cov") else a.assert_property)(p, name=n)
            for n, p in props.items()}


def norm(errors):
    return sorted((f.name.split(".", 1)[1], f.time, tuple(f.start_times), f.at_end) for f in errors)


def strip(stats):
    return {k.split(".", 1)[1]: v for k, v in stats.items()}


@cocotb.test()
async def test_derived_clock_preponed(dut):
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk_div, sampling="preponed"), name="p")
    hs = add(a, DIV_PROPS)
    await traffic(dut, 600)
    assert a.finalize(raise_on_failure=False) == [], a.failures[:3]
    st = a.stats()
    assert st["p.cap"]["passes"] > 250 and st["p.seen_imp"]["passes"] > 50, st
    assert st["p.seen_next"]["passes"] > 50, st
    dom = hs["cap"].domain
    assert dom.sampling == "preponed" and dom.unsnapped == 0, (dom.sampling, dom.unsnapped)


@cocotb.test()
async def test_derived_clock_edge_sampling_is_wrong(dut):
    """The same properties with values read at the clock edge: the race this
    DUT is built to show (if this passed, the other tests would prove nothing)."""
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk_div, sampling="edge"), name="e")
    add(a, DIV_PROPS)
    await traffic(dut, 600)
    a.finalize(raise_on_failure=False)
    failed = {f.name for f in a.failures}
    # (which of the others fail too depends on the simulator: the order in which it
    # updates the signals of the clk domain and calls back for clk_div)
    assert "e.cap" in failed, failed


@cocotb.test()
async def test_derived_clock_auto(dut):
    """Default sampling notices the race at the first difference and no tick
    is evaluated with values from after the edge."""
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk_div), name="a")
    hs = add(a, DIV_PROPS)
    assert hs["cap"].domain.sampling == "auto"
    await traffic(dut, 600)
    assert a.finalize(raise_on_failure=False) == [], a.failures[:3]
    assert hs["cap"].domain.sampling == "preponed"
    assert a.stats()["a.cap"]["passes"] > 250


def _cnt_i_before_now():
    # clk_int rises at 7, 21, 35, ... ns: edges before the current time step
    return ((int(get_sim_time("ns")) - 7) // 14) & 0xFF


@cocotb.test()
async def test_generated_clock(dut):
    """A clock generated inside the HDL: its counter sampled at tick k is k
    (on Verilator a read at the edge already returns k + 1)."""
    await setup(dut)
    res = {}
    for mode in ("preponed", "auto"):
        a = Assertions(dut, clock=posedge(dut.clk_int, sampling=mode), name=mode)
        h = a.assert_property("cnt_i == expected", name="count", values={"expected": _cnt_i_before_now})
        a.assert_property("1 |=> cnt_i == $past(cnt_i) + 8'd1", name="inc")
        res[mode] = (a, h)
    await Timer(14 * 400, unit="ns")
    for mode, (a, h) in res.items():
        assert a.finalize(raise_on_failure=False) == [], (mode, a.failures[:3])
        assert h.stats["passes"] > 390, h.stats
    # auto has decided by now
    assert res["auto"][1].domain.sampling in ("edge", "preponed")
    assert res["preponed"][1].domain.unsnapped == 0


@cocotb.test()
async def test_cocotb_clock_edge_equals_preponed(dut):
    """On a clock driven from cocotb both samplings give the same results,
    failure for failure, and auto settles on edge sampling."""
    await setup(dut)
    props = {
        "q": "req |=> req_q",
        "qq": "req ##1 req |=> req_qq",
        "never_twice": "req |=> !req",                 # fails
        "past": "$rose(req_q) |-> $past(req) && !$past(req, 2)",
        "cnt": "cnt == $past(cnt) + 8'd1",
        "cov": "req ##1 !req ##1 req",
    }
    groups = {m: Assertions(dut, clock=posedge(dut.clk, sampling=m), name=m)
              for m in ("edge", "preponed", "auto")}
    hs = {m: add(g, props) for m, g in groups.items()}
    await traffic(dut, 500, seed=3)
    res = {m: (g.finalize(raise_on_failure=False), g.stats()) for m, g in groups.items()}
    assert res["edge"][0], "the failing property did not fail"
    for m in ("preponed", "auto"):
        assert norm(res[m][0]) == norm(res["edge"][0]), m
        assert strip(res[m][1]) == strip(res["edge"][1]), m
    assert hs["auto"]["q"].domain.sampling == "edge"
    assert hs["preponed"]["q"].domain.unsnapped == 0


@cocotb.test()
async def test_preponed_idle_sleep(dut):
    """Idle sleep with preponed sampling on the derived clock: a domain that
    sleeps and one that does not give the same results. The watched signals
    belong to the clk domain and change before the clk_div edge is seen."""
    await setup(dut)
    props = dict(DIV_PROPS)
    del props["cap"]                       # reads a counter: never idle
    props["fails"] = "req_qq |=> !seen2"   # fails after two requests in a row
    props["cov"] = "req_qq ##1 req_qq"
    groups = {}
    for key, clk in (("sleep", posedge(dut.clk_div, sampling="preponed")),
                     ("awake", posedge(dut.clk_div, sampling="preponed").iff("1'b1"))):
        groups[key] = g = Assertions(dut, clock=clk, name=key)
        add(g, props)
    sleep_dom = groups["sleep"].assertions[0].domain
    awake_dom = groups["awake"].assertions[0].domain
    assert sleep_dom is not awake_dom
    awake_dom.idle_sleep = False
    rnd = random.Random(11)
    mid = None
    for burst in range(14):
        for _ in range(rnd.randint(2, 12)):
            await cycle(dut, req=int(rnd.random() < 0.6))
        for _ in range(rnd.randint(1, 40)):
            await cycle(dut)
        if burst == 7:
            mid = {k: strip(g.stats()) for k, g in groups.items()}
    for _ in range(10):
        await cycle(dut)
    res = {k: (g.finalize(raise_on_failure=False), g.stats()) for k, g in groups.items()}
    assert mid["sleep"] == mid["awake"]
    assert norm(res["sleep"][0]) == norm(res["awake"][0])
    assert strip(res["sleep"][1]) == strip(res["awake"][1])
    names = {f.name for f in res["sleep"][0]}
    assert names == {"sleep.fails"}, names
    if sleep_dom.idle_sleep:
        assert sleep_dom.sleeps > 5, sleep_dom.sleeps
        # (wake_ticks: clk_div edges seen after the wake-up of their time step; the order
        # of the two callbacks is the simulator's choice)
        dut._log.info("sleeps=%d wake_ticks=%d", sleep_dom.sleeps, sleep_dom.wake_ticks)
    assert awake_dom.sleeps == 0
    assert sleep_dom.unsnapped == 0 and awake_dom.unsnapped == 0
