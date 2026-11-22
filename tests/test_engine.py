"""Directed engine tests (trace runner, no simulator)."""
import pytest

from cocotbext.sva._errors import SvaError, SvaNameError, SvaUnsupportedError
from cocotbext.sva._trace import run_trace


def rows_of(n, **sets):
    """n rows; each keyword maps a 1-bit signal name to the set of ticks where it is 1."""
    return [{k: int(t in v) for k, v in sets.items()} for t in range(n)]


# Phase 0 reference example: results observed on a commercial SVA simulator
# (tick k there is time 10k+15 ns).
def test_matches_reference_simulator_example():
    rows = rows_of(17, req={1, 4, 8, 9}, ack={3, 5}, never=set())
    r = run_trace({
        "a_pass": "req |-> ##[1:3] ack",
        "a_fail": "req |-> ##1 ack",
        "a_vac": "never |-> ack",
        "c_cov": "req ##[1:3] ack",
    }, {"req": 1, "ack": 1, "never": 1}, rows, kinds={"c_cov": "cover"})
    st = r.stats
    assert (st["a_pass"]["failures"], st["a_pass"]["passes"], st["a_pass"]["vacuous"]) == (2, 2, 13)
    assert (st["a_fail"]["failures"], st["a_fail"]["passes"], st["a_fail"]["vacuous"]) == (3, 1, 13)
    assert (st["a_vac"]["vacuous"], st["a_vac"]["attempts"]) == (17, 17)
    assert r.fails("a_fail") == [(2, (1,), 1), (9, (8,), 1), (10, (9,), 1)]
    assert r.fails("a_pass") == [(11, (8,), 1), (12, (9,), 1)]
    assert [e.tick for e in r.events if e.assertion == "c_cov" and e.kind == "pass"] == [3, 5]
    assert st["c_cov"]["failures"] == 0


def test_local_variable_tag_check():
    sig = {"req": 1, "tag": 4, "rsp": 1, "rtag": 4}
    rows = [dict(req=0, tag=0, rsp=0, rtag=0) for _ in range(8)]
    rows[1].update(req=1, tag=5)
    rows[2].update(req=1, tag=6)
    rows[3].update(rsp=1, rtag=5)
    rows[5].update(rsp=1, rtag=7)        # tag 6 never answered within 3 cycles
    r = run_trace({"p": "(req, t = tag) |-> ##[1:3] (rsp && rtag == t)"}, sig, rows)
    assert r.stats["p"]["passes"] == 1
    assert r.fails("p") == [(5, (2,), 1)]


def test_disable_iff_cancels_attempts():
    rows = rows_of(8, a={1}, b={6}, rst={3})
    r = run_trace({"p": "disable iff (rst) a |-> ##[1:5] b"}, {"a": 1, "b": 1, "rst": 1}, rows)
    st = r.stats["p"]
    assert st["failures"] == 0 and st["passes"] == 0
    assert st["disabled"] == 2          # the in-flight attempt + the attempt starting at tick 3


def test_attempts_fail_at_their_own_deadlines():
    rows = rows_of(10, req={0, 1, 2}, ack=set())
    r = run_trace({"p": "req |-> ##[1:2] ack"}, {"req": 1, "ack": 1}, rows)
    assert r.fails("p") == [(2, (0,), 1), (3, (1,), 1), (4, (2,), 1)]


def test_identical_attempts_merge_and_keep_their_starts():
    # Both attempts wait for ack forever: their states become identical and merge;
    # at the end the strong obligation fails once with count 2 and both starts.
    rows = rows_of(5, req={0, 1}, ack=set())
    r = run_trace({"p": "req |-> strong(##[1:$] ack)"}, {"req": 1, "ack": 1}, rows)
    assert r.fails("p") == [(5, (0, 1), 2)]
    assert r.stats["p"]["failures"] == 2


def test_strong_and_weak_at_end_of_trace():
    rows = rows_of(4, a={2}, b=set())
    sig = {"a": 1, "b": 1}
    weak = run_trace({"p": "a |-> ##[1:$] b"}, sig, rows)
    strong = run_trace({"p": "a |-> strong(##[1:$] b)"}, sig, rows)
    assert weak.fails("p") == []
    assert strong.fails("p") == [(4, (2,), 1)]


def test_cover_counts_matches_not_failures():
    rows = rows_of(6, a={0, 2}, b={1})
    r = run_trace({"c": "a ##1 b"}, {"a": 1, "b": 1}, rows, kinds={"c": "cover"})
    assert r.stats["c"]["passes"] == 1 and r.stats["c"]["failures"] == 0


def test_named_sequences_with_arguments():
    from cocotbext.sva._compile import NamedDef
    from cocotbext.sva._parser import parse_sequence_or_property
    hs = NamedDef("hs", parse_sequence_or_property("x ##1 y"), ("x", "y"))
    rows = rows_of(6, a={0}, b={1}, c={2})
    r = run_trace({"p": "a |=> hs(b, c)", "q": "a |=> hs(c, b)"},
                  {"a": 1, "b": 1, "c": 1}, rows, named={"hs": hs})
    assert r.stats["p"]["passes"] == 1 and r.stats["q"]["failures"] == 1


def test_if_else_and_case():
    rows = rows_of(4, m={0}, a={0, 1}, b=set())
    sig = {"m": 1, "a": 1, "b": 1}
    r = run_trace({"p": "if (m) a ##1 a else b"}, sig, rows)
    assert r.stats["p"]["passes"] == 1 and r.stats["p"]["failures"] == 3
    r = run_trace({"p": "case (m) 1: a; default: 1; endcase"}, sig, rows)
    assert r.stats["p"]["passes"] == 4


def test_warning_for_sequence_that_never_matches():
    r = run_trace({"p": "(a ##1 b) intersect c |-> d"}, {"a": 1, "b": 1, "c": 1, "d": 1}, rows_of(2))
    assert any("can never match" in w for w in r.warnings)


def test_errors():
    with pytest.raises(SvaNameError, match="did you mean 'ack'"):
        run_trace({"p": "req |-> akc"}, {"req": 1, "ack": 1}, [])
    with pytest.raises(SvaError, match="empty match"):
        run_trace({"p": "a[*0:1]"}, {"a": 1}, [])
    with pytest.raises(SvaUnsupportedError, match="local variables"):
        run_trace({"p": "a |-> s.triggered"}, {"a": 1, "x": 3}, [],
                  named={"s": "(a, v = x) ##1 x == v"})
