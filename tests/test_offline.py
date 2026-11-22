"""Offline checking of VCD and FST dumps (TraceSampler) must give the results
of the trace runner on the same samples. FST files are made from the VCD
files by GTKWave's vcd2fst (an independent writer); those tests are skipped
when vcd2fst or the compiled kernel's FST reader is not available."""
import random
import shutil
import subprocess

import pytest

from cocotbext.sva import SvaError, TraceSampler
from cocotbext.sva._errors import SvaNameError
from cocotbext.sva._trace import run_trace
from test_fuzz import SIGNALS, Gen, random_trace

_CODES = "!#$%&'()*+,-./:;<=>?@"


def _fst_available():
    try:
        import cocotbext_sva_kernel as k
    except ImportError:
        return False
    return hasattr(k, "FstFile") and shutil.which("vcd2fst") is not None


FORMATS = ["vcd", pytest.param("fst", marks=pytest.mark.skipif(
    not _fst_available(), reason="needs vcd2fst and the kernel's FST reader"))]


def as_format(path, fmt):
    """The dump at path (VCD), or an FST conversion of it."""
    if fmt == "vcd":
        return path
    fst = path.with_suffix(".fst")
    subprocess.run(["vcd2fst", str(path), str(fst)], check=True, capture_output=True)
    return fst


def write_vcd(path, rows, signals, scope=("tb", "dut"), timescale="1ns", period=10, names=None,
              blast=()):
    """A dump in which every signal changes in the same time step as the
    rising clock edge (like a register): the value sampled at edge k is rows[k].
    names maps signal -> name written in the dump (default: the same).
    Vectors in `blast` are dumped one bit per variable, as some simulators do
    (alternately as "x [2]" and "x[2]")."""
    names = names or {}
    codes = {s: _CODES[i + 1] for i, s in enumerate(signals)}
    bitcodes = {}
    out = [f"$timescale {timescale} $end"]
    out += [f"$scope module {s} $end" for s in scope]
    out.append(f"$var wire 1 {_CODES[0]} clk $end")
    for k, (s, w) in enumerate(signals.items()):
        if s in blast:
            for i in reversed(range(w)):
                bitcodes[s, i] = c = f"{codes[s]}{i}"
                sep = " " if k % 2 else ""
                out.append(f"$var wire 1 {c} {names.get(s, s)}{sep}[{i}] $end")
            continue
        rng = f" [{w - 1}:0]" if w > 1 else ""
        out.append(f"$var wire {w} {codes[s]} {names.get(s, s)}{rng} $end")
    out += ["$upscope $end"] * len(scope) + ["$enddefinitions $end"]

    def val(s, row):
        w, v, xm = signals[s], row.get(s, 0), row.get((s, "x"), 0)
        bits = "".join("x" if xm >> i & 1 else str(v >> i & 1) for i in reversed(range(w)))
        if s in blast:
            return "\n".join(f"{b}{bitcodes[s, i]}" for b, i in zip(bits, reversed(range(w))))
        return f"{bits}{codes[s]}" if w == 1 else f"b{bits} {codes[s]}"

    out += ["#0", "$dumpvars", f"0{_CODES[0]}"] + [val(s, rows[0]) for s in signals] + ["$end"]
    for k in range(len(rows)):
        t = k * period + period // 2
        out.append(f"#{t}")
        out.append(f"1{_CODES[0]}")
        if k + 1 < len(rows):
            out += [val(s, rows[k + 1]) for s in signals if (rows[k + 1].get(s, 0), rows[k + 1].get((s, "x"), 0))
                    != (rows[k].get(s, 0), rows[k].get((s, "x"), 0))]
        out += [f"#{t + period // 2}", f"0{_CODES[0]}"]
    path.write_text("\n".join(out) + "\n")


def _expected(props, rows, kinds):
    r = run_trace(props, SIGNALS, rows, kinds=kinds, initial=rows[0])
    fails = sorted((e.assertion, -1 if e.kind == "end_fail" else e.tick, e.starts, e.count,
                    e.kind == "end_fail") for e in r.events if e.kind in ("fail", "end_fail"))
    return fails, r.stats


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("seed", range(4))
def test_offline_matches_trace_runner(tmp_path, seed, fmt):
    rnd = random.Random(seed)
    g = Gen(rnd, temporal=True)
    checked = 0
    for case in range(40):
        props = {f"p{k}": g.prop(rnd.randint(1, 3)) for k in range(rnd.randint(1, 4))}
        kinds = {n: rnd.choice(["assert", "assert", "cover"]) for n in props}
        rows = random_trace(rnd, rnd.randint(4, 30))
        try:
            fails, stats = _expected(props, rows, kinds)
        except SvaError:
            continue
        path = tmp_path / f"c{case}.vcd"
        write_vcd(path, rows, SIGNALS, blast=("x", "y") if seed == 3 else ())   # seed 3: bit by bit
        ts = TraceSampler(as_format(path, fmt), clock="posedge clk", scope="tb.dut")
        for n, text in props.items():
            (ts.cover_property if kinds[n] == "cover" else ts.assert_property)(text, name=n)
        res = ts.run()
        assert res.cycles == len(rows)
        # edge k is at time 10k + 5 ns
        got = sorted((f.assertion, -1 if f.at_end else (round(f.time_ns) - 5) // 10,
                      tuple((round(t) - 5) // 10 for t in f.start_times_ns), f.count, f.at_end)
                     for f in res.failures)
        assert got == fails, props
        for n in props:
            exp = stats[n]
            st = res.stats[n]
            if kinds[n] == "cover":
                assert st == {"attempts": exp["attempts"], "hits": exp["passes"]}
            else:
                assert st == exp
        checked += 1
    assert checked > 20


@pytest.mark.parametrize("fmt", FORMATS)
def test_unknown_values_timescale_and_names(tmp_path, fmt):
    rows = [{"a": 0, "b": 0}, {"a": 1, "b": 0}, {"a": 0, ("a", "x"): 1, "b": 1}, {"a": 0, "b": 0}]
    path = tmp_path / "w.vcd"
    write_vcd(path, rows, {"a": 1, "b": 1}, timescale="100ps", names={"a": "A_Sig"})
    ts = TraceSampler(as_format(path, fmt), clock="clk", scope="tb.dut")
    ts.assert_property("!$isunknown(a_sig)", name="known")
    ts.assert_property("a_sig |=> b", name="ab")
    res = ts.run()
    assert [(f.assertion, f.time_ns) for f in res.failures] == [("known", 2.5)]   # edge 2 at 25 x 100 ps
    assert res.stats["ab"]["passes"] == 1
    assert not res.ok
    with pytest.raises(AssertionError, match="known FAILED at 2.5 ns"):
        res.check()


def test_disable_iff_negedge_and_errors(tmp_path):
    rows = [{"a": 1, "b": 0}, {"a": 1, "b": 0}, {"a": 0, "b": 1}, {"a": 1, "b": 1}]
    path = tmp_path / "w.vcd"
    write_vcd(path, rows, {"a": 1, "b": 1})
    ts = TraceSampler(path, clock="posedge clk", scope="tb.dut", disable_iff="b")
    ts.assert_property("a |-> b", name="ab")          # fails at edge 0 and 1; disabled at 2, 3
    res = ts.run()
    assert [f.time_ns for f in res.failures] == [5, 15]
    assert res.stats["ab"]["disabled"] == 2
    # negedge: edge k is at 10k + 10; signals changed at the rising edge before
    neg = TraceSampler(path, clock="negedge clk", scope="tb.dut")
    neg.assert_property("a", name="a")
    res = neg.run()
    assert [f.time_ns for f in res.failures] == [20]   # row 2 (a=0) is sampled at the negedge at 20 ns
    bad = TraceSampler(path, clock="posedge clk", scope="tb.dut")
    bad.assert_property("aa |-> b")
    with pytest.raises(SvaNameError, match="did you mean"):
        bad.run()
    with pytest.raises(SvaError, match="clock 'nope' not found"):
        TraceSampler(path, clock="posedge nope").run()


@pytest.mark.parametrize("fmt", FORMATS)
def test_corpus_replays_through_dumps(tmp_path, fmt):
    """Every golden-corpus case (except single-attempt ones, which have no
    offline form) written as a dump and checked offline: failures and
    counters equal the trace runner's, which test_corpus checks against the
    reference tool."""
    from test_corpus import DATA
    if not DATA["cases"]:
        pytest.skip("no corpus")
    signals = DATA["signals"]
    zeros = {s: 0 for s in signals}
    checked = 0
    for i, case in enumerate(DATA["cases"]):
        if case.get("once") or (fmt == "fst" and i % 4):    # FST: every 4th case (one vcd2fst each)
            continue
        rows = [zeros] + case["trace"]
        props, kinds = case["props"], case["kinds"]
        r = run_trace(props, signals, rows, kinds=kinds, initial=zeros)
        fails = sorted((e.assertion, -1 if e.kind == "end_fail" else e.tick, e.starts, e.count,
                        e.kind == "end_fail") for e in r.events if e.kind in ("fail", "end_fail")
                       and kinds[e.assertion] != "cover")
        path = tmp_path / f"{case['id']}.vcd"
        write_vcd(path, rows, signals)
        dump = as_format(path, fmt)
        ts = TraceSampler(dump, clock="posedge clk", scope="tb.dut")
        for n, text in props.items():
            (ts.cover_property if kinds[n] == "cover" else ts.assert_property)(text, name=n)
        res = ts.run()
        got = sorted((f.assertion, -1 if f.at_end else (round(f.time_ns) - 5) // 10,
                      tuple((round(t) - 5) // 10 for t in f.start_times_ns), f.count, f.at_end)
                     for f in res.failures)
        assert got == fails, case["id"]
        for n in props:
            st = r.stats[n]
            exp = {"attempts": st["attempts"], "hits": st["passes"]} if kinds[n] == "cover" else st
            assert res.stats[n] == exp, (case["id"], n)
        path.unlink()
        if dump != path:
            dump.unlink()
        checked += 1
    assert checked > (700 if fmt == "vcd" else 170)


def test_fst_without_kernel_is_a_clear_error(tmp_path, monkeypatch):
    import builtins
    path = tmp_path / "w.fst"
    path.write_bytes(b"\x00" + bytes(300))       # an FST file starts with a 0 byte (header block)
    real_import = builtins.__import__

    def no_kernel(name, *a, **kw):
        if name == "cocotbext_sva_kernel":
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_kernel)
    ts = TraceSampler(path, clock="posedge clk")
    ts.assert_property("a")
    with pytest.raises(SvaError, match="reading FST needs the compiled kernel"):
        ts.run()


def write_mc_vcd(path, rows, signals, clocks, scope=("tb", "dut")):
    """A dump with several clocks: at time step k (10k + 5 ns) the clocks
    whose flag is set in rows[k] rise (and fall 3 ns later), and the signals
    change to rows[k + 1] in the same time step: the values sampled at step
    k are rows[k]."""
    names = list(clocks) + list(signals)
    codes = {s: _CODES[i] for i, s in enumerate(names)}
    out = ["$timescale 1ns $end"] + [f"$scope module {s} $end" for s in scope]
    for c in clocks:
        out.append(f"$var wire 1 {codes[c]} {c} $end")
    for s, w in signals.items():
        out.append(f"$var wire {w} {codes[s]} {s}{f' [{w - 1}:0]' if w > 1 else ''} $end")
    out += ["$upscope $end"] * len(scope) + ["$enddefinitions $end"]

    def val(s, row):
        w, v = signals[s], row.get(s, 0)
        return f"{v & 1}{codes[s]}" if w == 1 else f"b{v:0{w}b} {codes[s]}"

    out += ["#0", "$dumpvars"] + [f"0{codes[c]}" for c in clocks] + [val(s, rows[0]) for s in signals]
    out.append("$end")
    for k, row in enumerate(rows):
        t = 10 * k + 5
        out.append(f"#{t}")
        out += [f"1{codes[c]}" for c in clocks if row[c]]
        if k + 1 < len(rows):
            out += [val(s, rows[k + 1]) for s in signals if rows[k + 1].get(s, 0) != row.get(s, 0)]
        if any(row[c] for c in clocks):
            out += [f"#{t + 3}"] + [f"0{codes[c]}" for c in clocks if row[c]]
    path.write_text("\n".join(out) + "\n")


def _flat(events, steps):
    """[(assertion, step of the verdict or -1 at the end, step of a start)]."""
    out = []
    for name, tick, starts, at_end in events:
        out += [(name, -1 if at_end else steps[tick], steps[s]) for s in starts]
    return sorted(out)


def test_multiclock_corpus_through_dumps(tmp_path, monkeypatch):
    """Every multi-clock corpus case, with a first time step where all
    clocks tick on zeros, written as a dump with three clocks and checked
    offline: failures and counters must equal the trace runner's on the
    same samples (assertions on one clock are then evaluated on that clock's
    ticks only, multi-clock ones on the ticks of their clocks)."""
    from cocotbext.sva import _engine
    from test_corpus_mc import DATA
    monkeypatch.setattr(_engine, "MAX_STARTS", 1 << 30)
    signals, clocks = DATA["signals"], DATA["clocks"]
    zeros = dict({s: 0 for s in signals}, **{c: 1 for c in clocks})
    checked = 0
    for case in DATA["cases"]:
        rows = [zeros] + case["trace"]
        props, kinds = case["props"], case["kinds"]
        steps = [k for k, row in enumerate(rows) if any(row[c] for c in clocks)]
        try:
            r = run_trace(props, dict(signals, **{c: 1 for c in clocks}), [rows[k] for k in steps],
                          kinds=kinds, initial=zeros, clock=clocks[0], force_multiclock=True)
        except SvaError:
            continue
        want = _flat([(e.assertion, e.tick, e.starts, e.kind == "end_fail") for e in r.events
                      if e.kind in ("fail", "end_fail") and kinds[e.assertion] != "cover"], steps)
        path = tmp_path / f"{case['id']}.vcd"
        write_mc_vcd(path, rows, signals, clocks)
        ts = TraceSampler(path, clock=f"posedge {clocks[0]}", scope="tb.dut")
        for n, text in props.items():
            (ts.cover_property if kinds[n] == "cover" else ts.assert_property)(text, name=n)
        res = ts.run()
        k_of = {10 * k + 5: k for k in range(len(rows))}
        got = sorted((f.assertion, -1 if f.at_end else k_of[round(f.time_ns)], k_of[round(t)])
                     for f in res.failures for t in f.start_times_ns)
        assert got == want, case["id"]
        for n in props:
            st = r.stats[n]
            exp = {"attempts": st["attempts"], "hits": st["passes"]} if kinds[n] == "cover" else st
            assert res.stats[n] == exp, (case["id"], n)
        path.unlink()
        checked += 1
    assert checked > 300


def test_property_clocks_offline(tmp_path):
    """A clock written in the property selects the clock (edge, iff); the
    sampler's clock is the default for the parts that have none."""
    #          k:   0  1  2  3  4  5  6  7
    clk = [1, 1, 1, 1, 1, 1, 1, 1]
    clk2 = [0, 1, 0, 0, 1, 0, 1, 0]
    a = [0, 1, 0, 0, 1, 0, 0, 1]
    b = [0, 0, 1, 0, 0, 1, 1, 0]
    en = [1, 1, 1, 1, 0, 1, 1, 1]
    rows = [{"clk": clk[k], "clk2": clk2[k], "a": a[k], "b": b[k], "en": en[k]} for k in range(8)]
    path = tmp_path / "w.vcd"
    write_mc_vcd(path, rows, {"a": 1, "b": 1, "en": 1}, ["clk", "clk2"])
    ts = TraceSampler(path, clock="posedge clk", scope="tb.dut")
    ts.assert_property("@(posedge clk2) a", name="on2")              # clk2 ticks at 1, 4, 6
    ts.assert_property("@(posedge clk2 iff en) a", name="on2_en")    # not at 4 (en = 0)
    ts.assert_property("@(negedge clk2) !b", name="neg2")            # at 18, 48, 68 ns: rows 2, 5, 7
    ts.assert_property("a |=> @(posedge clk2) b", name="mc")         # a at 1 -> clk2 at 4: b = 0
    ts.assert_property("@(posedge clk) a |=> @(posedge clk) b", name="same")   # one clock, holds
    ts.cover_property("@(posedge clk2) a ##1 @(posedge clk) b", name="cov")
    ts.assert_property("@(posedge clk2) a |-> s_eventually (b && !b)", name="end")   # pending
    res = ts.run()
    # failures at the end of the dump: at its last tick (clk at 75 ns)
    assert [(f.time_ns, f.start_times_ns, f.count) for f in res.failures if f.at_end] ==         [(75.0, [15.0, 45.0], 2)]                   # (the two attempts merged)
    fails = sorted((f.assertion, f.time_ns, tuple(f.start_times_ns)) for f in res.failures
                   if not f.at_end)
    assert fails == [("mc", 45.0, (15.0,)), ("neg2", 18.0, (18.0,)), ("neg2", 48.0, (48.0,)),
                     ("on2", 65.0, (65.0,)), ("on2_en", 65.0, (65.0,))], fails
    assert res.stats["on2"]["attempts"] == 3 and res.stats["on2_en"]["attempts"] == 2
    assert res.stats["neg2"]["attempts"] == 3 and res.stats["same"]["passes"] == 2
    assert res.stats["mc"]["attempts"] == 8 and res.stats["mc"]["vacuous"] == 5
    assert res.stats["cov"] == {"attempts": 3, "hits": 2}
    assert res.cycles == 8 + 3              # clk at every step, plus the falling edges of clk2
    early = ts.run(until_ns=40)             # time steps up to 40 ns only; the end is at 40 ns
    assert [(f.assertion, f.time_ns, f.at_end) for f in early.failures] ==         [("neg2", 18.0, False), ("end", 40.0, True)]
    assert early.cycles == 4 + 1 and early.stats["mc"]["attempts"] == 4
    with pytest.raises(SvaError, match="clock 'nope' not found"):
        bad = TraceSampler(path, clock="posedge clk", scope="tb.dut")
        bad.assert_property("a |=> @(posedge nope) b")
        bad.run()


def test_property_with_arguments_offline(tmp_path):
    """A Property called with actual arguments checks the actual signals."""
    from cocotbext.sva import Property, Sequence
    rows = [{"req": 1, "gnt": 0, "rst": 0}, {"req": 1, "gnt": 1, "rst": 0},
            {"req": 0, "gnt": 0, "rst": 1}, {"req": 1, "gnt": 0, "rst": 1}]
    path = tmp_path / "w.vcd"
    write_vcd(path, rows, {"req": 1, "gnt": 1, "rst": 1})
    HANDSHAKE = Property("x |-> y", args=("x", "y"))

    def failures(prop, **kw):
        ts = TraceSampler(path, clock="posedge clk", scope="tb.dut", **kw)
        ts.assert_property(prop, name="p")
        return [f.time_ns for f in ts.run().failures]

    assert failures(HANDSHAKE(x="req", y="gnt")) == failures("req |-> gnt") == [5, 35]
    assert failures(HANDSHAKE("req", "!gnt")) == failures("req |-> !gnt") == [15]
    # the default disable iff applies to it as to text
    assert failures(HANDSHAKE("req", "gnt"), disable_iff="rst") == [5]
    # a Property without arguments, and a Sequence in a cover
    assert failures(Property("req |-> gnt")) == [5, 35]
    ts = TraceSampler(path, clock="posedge clk", scope="tb.dut")
    ts.cover_property(Sequence("x ##1 x", args=("x",))("req"), name="c")
    assert ts.run().stats["c"]["hits"] == 1
    # errors: arguments still to give, and a handle (offline needs names)
    ts = TraceSampler(path, clock="posedge clk", scope="tb.dut")
    with pytest.raises(SvaError, match="call it with actual arguments"):
        ts.assert_property(HANDSHAKE)
    with pytest.raises(SvaError, match="names or expressions"):
        ts.assert_property(HANDSHAKE(x=object(), y="gnt"))
