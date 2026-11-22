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


# --------------------------------------------------------------------------
# Multi-clock properties
# --------------------------------------------------------------------------

def _int_edge_after(t):
    """Time of the first rising edge of clk_int strictly after t (7, 21, 35, ... ns)."""
    k = (int(t) - 7) // 14 + 1
    return 7 + 14 * max(k, 0)


async def _sends(dut, n, seed):
    """Send n words, far enough apart for the synchroniser; returns
    [(time of the clk edge that samples send, word)]."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        word = rnd.randint(0, 255)
        await cycle(dut, send=1, din=word)
        out.append((get_sim_time("ns"), word))
        for _ in range(rnd.randint(8, 12)):
            await cycle(dut)
    return out


@cocotb.test()
async def test_multiclock_handshake(dut):
    """A word sent in the clk domain arrives in the clk_int domain: the
    request toggle is seen by the first clk_int edge strictly after the clk
    edge (|=>), and valid_b follows three clk_int ticks later."""
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk), name="mc")
    arrives = a.assert_property("send |=> @(posedge clk_int) ##3 valid_b", name="arrives")
    data = a.assert_property("(send, v = din) |=> @(posedge clk_int) ##3 (valid_b && data_b == v)",
                             name="data")
    early = a.assert_property("send |=> @(posedge clk_int) ##2 valid_b", name="early")   # fails
    cov = a.cover_property("send ##1 @(posedge clk_int) 1 ##3 valid_b ##1 !valid_b", name="cov")
    back = a.assert_property("@(posedge clk_int) $rose(valid_b) |-> @(posedge clk) $stable(data_a)",
                             name="back")
    sent = await _sends(dut, 12, seed=5)
    for _ in range(6):
        await cycle(dut)
    a.finalize(raise_on_failure=False)
    failed = {f.name for f in a.failures}
    assert failed == {"mc.early"}, failed
    assert arrives.stats["passes"] == len(sent) and data.stats["passes"] == len(sent), a.stats()
    assert cov.stats["hits"] == len(sent), cov.stats
    assert back.stats["passes"] == len(sent), back.stats
    # attempts are counted at the ticks of the leading clock
    assert arrives.domain is early.domain and arrives.domain is back.domain   # one set of clocks
    assert 100 < arrives.stats["attempts"] < 160 and arrives.stats["attempts"] == early.stats["attempts"]
    # each failure of `early` at the third clk_int edge after its clk edge
    got = sorted((f.time, f.start_times[0]) for f in a.failures)
    want = sorted((_int_edge_after(t) + 28, t) for t, _ in sent)
    assert got == want, (got, want)
    assert arrives.domain.unsnapped == 0


@cocotb.test()
async def test_multiclock_simultaneous_ticks(dut):
    """clk_div ticks in the same time step as every other clk tick. From such
    a clk tick, |=> goes to the next clk_div tick and |-> stays on this one."""
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk), name="sim")
    # clk_div low before a clk edge: clk_div rises with it
    nxt = a.assert_property("req && !clk_div |=> @(posedge clk_div) seen", name="next")
    same = a.assert_property("req && !clk_div |-> @(posedge clk_div) seen", name="same")     # fails
    both = a.cover_property("@(posedge clk_div) 1 ##0 @(posedge clk) 1", name="both")
    rnd = random.Random(4)
    rec = []                              # (time of the clk edge, req, clk_div before it)
    for _ in range(300):
        await FallingEdge(dut.clk)
        r = int(rnd.random() < 0.5)
        dut.req.value = r
        div = int(dut.clk_div.value)
        await RisingEdge(dut.clk)
        rec.append((get_sim_time("ns"), r, div))
    dut.req.value = 0
    for _ in range(4):
        await cycle(dut)
    a.finalize(raise_on_failure=False)
    assert {f.name for f in a.failures} == {"sim.same"}
    assert nxt.stats["passes"] > 50 and nxt.stats["failures"] == 0, nxt.stats
    # seen (before a clk_div edge) is req as sampled by the clk edge two cycles earlier
    # (req was 0 before the test)
    want = [(t, t) for i, (t, r, div) in enumerate(rec)
            if r and not div and (i < 2 or not rec[i - 2][1])]
    got = sorted((f.time, f.start_times[0]) for f in a.failures)
    assert got == want, (got[:5], want[:5], len(got), len(want))
    assert len(want) > 20
    # clk_div ticks together with clk every time it ticks (once per two clk cycles)
    assert 145 <= both.stats["hits"] <= 155, both.stats


@cocotb.test()
async def test_multiclock_expect_and_initial(dut):
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk), name="x")
    ev = a.assert_property("s_eventually (send ##1 @(posedge clk_int) 1 ##3 valid_b)", name="ev",
                           initial=True)
    never = a.assert_property("@(posedge clk) 1 |=> @(posedge clk_int) s_eventually (cnt_i == 8'd0 && valid_b && !valid_b)",
                              name="never", initial=True)     # strong, never satisfied
    for _ in range(3):
        await cycle(dut)
    await FallingEdge(dut.clk)
    dut.send.value = 1
    dut.din.value = 0x5A
    ok = cocotb.start_soon(a.expect("send ##1 @(posedge clk_int) 1 ##3 (valid_b && data_b == 8'h5A)",
                                    name="exp_ok"))
    bad = cocotb.start_soon(a.expect("send |=> @(posedge clk_int) valid_b", name="exp_bad"))
    await RisingEdge(dut.clk)
    for _ in range(12):
        await cycle(dut)
    assert ok.done() and bad.done()
    assert ok.result() is True and bad.result() is False
    errors = a.finalize(raise_on_failure=False)
    assert ev.stats == {"attempts": 1, "passes": 1, "vacuous": 0, "failures": 0, "disabled": 0,
                        "pending_fail": 0}, ev.stats
    names = sorted(f.name for f in errors)
    assert names == ["x.exp_bad", "x.never"], names
    assert [f.at_end for f in errors if f.name == "x.never"] == [True]


@cocotb.test()
async def test_same_clock_written_twice_is_single_clock(dut):
    """Clocking events that all name one clock do not make an assertion
    multi-clock: it shares the ordinary domain of that clock."""
    await setup(dut)
    a = Assertions(dut, clock=posedge(dut.clk), name="one")
    h1 = a.assert_property("@(posedge clk) req |=> @(posedge clk) req_q", name="twice")
    h2 = a.assert_property("req |=> req_q", name="plain")
    a.sequence("on_int", "@(posedge clk_int) valid_b ##1 !valid_b")
    h3 = a.cover_property("on_int", name="named")
    await traffic(dut, 100, seed=9)
    assert a.finalize(raise_on_failure=False) == []
    assert h1.domain is h2.domain
    assert h1.stats == h2.stats and h1.stats["passes"] > 20
    assert h3.domain is not h1.domain and h3.domain.label.endswith("clk_int"), h3.domain.label
