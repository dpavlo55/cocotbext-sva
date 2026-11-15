"""Offline checking of VCD dumps (TraceSampler) must give the results of the
trace runner on the same samples."""
import random

import pytest

from cocotbext.sva import SvaError, TraceSampler
from cocotbext.sva._errors import SvaNameError
from cocotbext.sva._trace import run_trace
from test_fuzz import SIGNALS, Gen, random_trace

_CODES = "!#$%&'()*+,-./:;<=>?@"


def write_vcd(path, rows, signals, scope=("tb", "dut"), timescale="1ns", period=10, names=None):
    """A dump in which every signal changes in the same time step as the
    rising clock edge (like a register): the value sampled at edge k is rows[k].
    names maps signal -> name written in the dump (default: the same)."""
    names = names or {}
    codes = {s: _CODES[i + 1] for i, s in enumerate(signals)}
    out = [f"$timescale {timescale} $end"]
    out += [f"$scope module {s} $end" for s in scope]
    out.append(f"$var wire 1 {_CODES[0]} clk $end")
    for s, w in signals.items():
        rng = f" [{w - 1}:0]" if w > 1 else ""
        out.append(f"$var wire {w} {codes[s]} {names.get(s, s)}{rng} $end")
    out += ["$upscope $end"] * len(scope) + ["$enddefinitions $end"]

    def val(s, row):
        w, v, xm = signals[s], row.get(s, 0), row.get((s, "x"), 0)
        bits = "".join("x" if xm >> i & 1 else str(v >> i & 1) for i in reversed(range(w)))
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


@pytest.mark.parametrize("seed", range(3))
def test_offline_matches_trace_runner(tmp_path, seed):
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
        write_vcd(path, rows, SIGNALS)
        ts = TraceSampler(path, clock="posedge clk", scope="tb.dut")
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


def test_unknown_values_timescale_and_names(tmp_path):
    rows = [{"a": 0, "b": 0}, {"a": 1, "b": 0}, {"a": 0, ("a", "x"): 1, "b": 1}, {"a": 0, "b": 0}]
    path = tmp_path / "w.vcd"
    write_vcd(path, rows, {"a": 1, "b": 1}, timescale="100ps", names={"a": "A_Sig"})
    ts = TraceSampler(path, clock="clk", scope="tb.dut")
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
