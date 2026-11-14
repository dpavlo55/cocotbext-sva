"""Phase 0 benchmarks.

read_cost : wall-clock cost of one signal read, per read method and width.
speed_*   : wall-clock time to simulate BENCH_CYCLES clock cycles under
            increasing Python load, from no Python at all to naive assertions.

Results are appended as JSON lines to BENCH_OUT.
"""
import json
import os
import time

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

SIM = os.environ.get("CALIB_SIM", "?")
LANG = os.environ.get("CALIB_LANG", "?")
CYCLES = int(os.environ.get("BENCH_CYCLES", "20000"))
PERIOD_NS = 10


def _record(kind, **data):
    line = {"sim": SIM, "lang": LANG, "kind": kind, **data}
    cocotb.log.info("BENCH %s", json.dumps(line))
    out = os.environ.get("BENCH_OUT")
    if out:
        with open(out, "a") as f:
            f.write(json.dumps(line) + "\n")


def _start_clock(dut):
    clock = Clock(dut.clk, PERIOD_NS, unit="ns", impl="gpi")
    clock.start()
    return clock


# --------------------------------------------------------------------------
# Read cost
# --------------------------------------------------------------------------

@cocotb.test()
async def read_cost(dut):
    clock = _start_clock(dut)
    await Timer(10 * PERIOD_NS, unit="ns")
    await ReadOnly()
    reads = 20000
    signals = {1: dut.s1, 8: dut.s8, 32: dut.s32, 64: dut.s64, 128: dut.s128, 512: dut.s512}
    for width, h in signals.items():
        ll = h._handle  # low-level GPI handle (private cocotb API)
        methods = {
            "value": lambda: h.value,
            "value->int": (lambda: int(h.value)) if width == 1 else (lambda: h.value.to_unsigned()),
            "gpi binstr": ll.get_signal_val_binstr,
            "gpi binstr->int": lambda: int(ll.get_signal_val_binstr(), 2),
        }
        if width <= 32:
            methods["gpi long"] = ll.get_signal_val_long
            # Is the raw integer the logical value? (VHPI returns enum positions
            # for std_logic: '0' -> 2, '1' -> 3.) Sample over several cycles.
            mismatches = []
            for _ in range(16):
                await RisingEdge(dut.clk)
                await ReadOnly()
                raw, true = ll.get_signal_val_long(), int(h.value)
                if raw != true:
                    mismatches.append((true, raw))
            _record("long_check", width=width, matches=not mismatches,
                    examples=sorted(set(mismatches))[:4])
        for name, fn in methods.items():
            fn()  # warm up
            t0 = time.perf_counter_ns()
            for _ in range(reads):
                fn()
            ns = (time.perf_counter_ns() - t0) / reads
            _record("read", width=width, method=name, ns_per_read=round(ns, 1))

    # Same reads, but made inside the clock-edge callback (where the engine
    # samples) instead of the ReadOnly phase: 200 reads per edge, 10 edges.
    edge = RisingEdge(dut.clk)
    for width, h in ((1, dut.req), (32, dut.s32)):
        ll = h._handle
        methods = {"value": lambda: h.value, "gpi binstr": ll.get_signal_val_binstr}
        if LANG == "verilog":
            methods["gpi long"] = ll.get_signal_val_long
        for name, fn in methods.items():
            total = 0
            for _ in range(10):
                await edge
                t0 = time.perf_counter_ns()
                for _ in range(200):
                    fn()
                total += time.perf_counter_ns() - t0
            _record("read_at_edge", width=width, method=name, ns_per_read=round(total / 2000, 1))

    # Cost of the FIRST read of a signal after each edge (one read per signal
    # per time step is the engine's real access pattern).
    for name in ("req", "ack", "s32"):
        ll = getattr(dut, name)._handle
        fn = ll.get_signal_val_long if LANG == "verilog" else ll.get_signal_val_binstr
        total = 0
        for _ in range(200):
            await edge
            t0 = time.perf_counter_ns()
            fn()
            total += time.perf_counter_ns() - t0
        _record("first_read", signal=name, ns_per_read=round(total / 200, 1))
    clock.stop()


# --------------------------------------------------------------------------
# Simulation speed under Python load
# --------------------------------------------------------------------------

async def _timed(dut, scenario, body, stats=None, **extra):
    clock = _start_clock(dut)
    await RisingEdge(dut.clk)
    t0 = time.perf_counter()
    await body()
    wall = time.perf_counter() - t0
    clock.stop()
    _record("speed", scenario=scenario, cycles=CYCLES, wall_s=round(wall, 4),
            cycles_per_s=round(CYCLES / wall), **extra, **(stats or {}))
    await Timer(2 * PERIOD_NS, unit="ns")


@cocotb.test()
async def speed_0_no_python(dut):
    """Baseline: the simulator runs CYCLES cycles, Python only waits on a Timer."""
    async def body():
        await Timer(CYCLES * PERIOD_NS, unit="ns")
    await _timed(dut, "0 no Python per cycle", body)


@cocotb.test()
async def speed_1_edge_loop(dut):
    """One coroutine wakes on every clock edge, does nothing."""
    async def body():
        edge = RisingEdge(dut.clk)
        for _ in range(CYCLES):
            await edge
    await _timed(dut, "1 wake every cycle", body)


@cocotb.test()
async def speed_2_edge_loop_reads(dut):
    """One coroutine per edge, reads 4 signals with the low-level API."""
    async def body():
        edge = RisingEdge(dut.clk)
        readers = [_bit_reader(dut.req), _bit_reader(dut.ack), _bit_reader(dut.s1),
                   _bit_reader(dut.req)]
        for _ in range(CYCLES):
            await edge
            for read in readers:
                read()
    await _timed(dut, "2 wake + 4 fast reads", body)


def _naive_assertions(dut, k, stats):
    """Coroutine-per-attempt implementation of `req |-> ##[1:3] ack`, k copies.

    This is what a straightforward Python port looks like: each assertion has a
    monitor coroutine that reads its antecedent every cycle (public API) and
    spawns a task per attempt, which waits up to 3 cycles for the consequent.
    """
    edge = RisingEdge(dut.clk)

    async def attempt():
        for _ in range(3):
            await edge
            if int(dut.ack.value):
                stats["pass"] += 1
                return
        stats["fail"] += 1

    async def monitor():
        while True:
            await edge
            if int(dut.req.value):
                cocotb.start_soon(attempt())

    return [cocotb.start_soon(monitor()) for _ in range(k)]


async def _naive(dut, k):
    stats = {"pass": 0, "fail": 0}

    async def body():
        tasks = _naive_assertions(dut, k, stats)
        await Timer(CYCLES * PERIOD_NS, unit="ns")
        for t in tasks:
            t.cancel()
    await _timed(dut, f"3 naive x{k}", body, stats=stats, assertions=k)


@cocotb.test()
async def speed_3_naive_10(dut):
    await _naive(dut, 10)


@cocotb.test()
async def speed_3_naive_50(dut):
    await _naive(dut, 50)


def _dfa_for_req_ack():
    """Lazy attempt-merging DFA for `req |-> ##[1:3] ack`.

    State = bitmask of pending attempts by age (bit i: attempt started i+1
    cycles ago and still waiting). Transitions are computed on first use and
    cached, as the planned engine does. Input bits: (req, ack).
    """
    cache = {}

    def step(state, req, ack):
        key = (state, req, ack)
        hit = cache.get(key)
        if hit is not None:
            return hit
        fails = 0
        nxt = 0
        if not ack:
            if state & 0b100:      # oldest attempt reached its last chance
                fails = 1
            nxt = (state << 1) & 0b110
        if req:
            nxt |= 0b001
        cache[key] = hit = (nxt, fails)
        return hit

    return step


def _bit_reader(h):
    """Fastest correct reader for a 1-bit signal, chosen once per handle.

    get_signal_val_long() is the cheapest GPI call, but it is only correct for
    Verilog (VPI) objects: VHPI implementations return enum positions or error
    values for std_logic. VHDL objects therefore use the binary-string read.
    Phase 1 replaces this with a per-interface/per-type table from calibration.
    """
    ll = h._handle
    if LANG == "verilog":
        return ll.get_signal_val_long
    get = ll.get_signal_val_binstr
    return lambda: 1 if get() in ("1", "H") else 0


async def _merged(dut, k):
    stats = {"attempts": 0, "fail": 0}

    async def body():
        edge = RisingEdge(dut.clk)
        read_req, read_ack = _bit_reader(dut.req), _bit_reader(dut.ack)
        steps = [_dfa_for_req_ack() for _ in range(k)]  # k independent assertions
        states = [0] * k
        for _ in range(CYCLES):
            await edge
            req = read_req()  # each signal read once, shared by all k assertions
            ack = read_ack()
            stats["attempts"] += req * k
            for i in range(k):
                states[i], f = steps[i](states[i], req, ack)
                if f:
                    stats["fail"] += f
    await _timed(dut, f"4 merged-DFA loop x{k}", body, stats=stats, assertions=k)


@cocotb.test()
async def speed_4_merged_10(dut):
    await _merged(dut, 10)


@cocotb.test()
async def speed_4_merged_50(dut):
    await _merged(dut, 50)


@cocotb.test()
async def python_speed(dut):
    """Pure-Python work inside the simulator process (no GPI calls).

    Compares the embedded interpreter's speed across simulators; the same loop
    run in a plain `python` process is the reference.
    """
    def work(n=300_000):
        d = {}
        acc = 0
        for i in range(n):
            k = (i & 7, i & 1)
            v = d.get(k)
            if v is None:
                d[k] = v = i & 3
            acc += v
        return acc

    work(10_000)
    best = min(_time(work) for _ in range(3))
    _record("python_speed", ns_per_iter=round(best * 1e9 / 300_000, 1))


def _time(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


async def _package(dut, k, mixed=False, idle=False):
    from cocotbext.sva import Assertions, posedge
    stats = {}

    async def body():
        a = Assertions(dut, clock=posedge(dut.clk), name="b")
        texts = ["req |-> ##[1:3] ack"]
        if idle:
            texts = ["rare |-> ##[1:3] !rare", "rare |=> !rare"]
        if mixed:
            texts = ["req |-> ##[1:3] ack", "req |=> ##1 ack", "ack |-> $past(req, 2)",
                     "$rose(req) |-> ##2 ack", "req ##1 req |-> ##1 ack ##1 ack"]
        for i in range(k):
            a.assert_property(texts[i % len(texts)], name=f"p{i}")
        await Timer(CYCLES * PERIOD_NS, unit="ns")
        errs = a.finalize(raise_on_failure=False)
        st = a.stats()
        stats["passes"] = sum(v.get("passes", 0) for v in st.values())
        stats["failures"] = len(errs)
    label = "mixed" if mixed else "idle" if idle else "same"
    await _timed(dut, f"5 cocotbext-sva x{k} {label}", body, stats=stats, assertions=k)


@cocotb.test()
async def speed_5_package_10(dut):
    await _package(dut, 10)


@cocotb.test()
async def speed_5_package_50(dut):
    await _package(dut, 50)


@cocotb.test()
async def speed_5_package_50_mixed(dut):
    await _package(dut, 50, mixed=True)


@cocotb.test()
async def speed_5_package_50_idle(dut):
    await _package(dut, 50, idle=True)
