"""Expression semantics: SV width/sign rules, selects, system functions."""
import pytest

from cocotbext.sva._trace import run_trace


def holds(expr, signals, row, params=None):
    """Evaluate `expr` as a one-tick property on `row`."""
    res = run_trace({"p": expr}, signals, [row], params=params)
    return any(e.kind == "pass" for e in res.events)


S8 = {"a": 8, "b": 8, "c": 4, "s": (8, True), "t": (8, True), "bit": 1}


@pytest.mark.parametrize("expr,row,want", [
    ("a + b == 0", {"a": 255, "b": 1}, False),           # 32-bit context (unsized 0): 256
    ("a + b == 8'd0", {"a": 255, "b": 1}, True),         # 8-bit context: wraps
    ("a + 8'd1 == 8'd0", {"a": 255}, True),
    ("c + 4'd1 == 4'd0", {"c": 15}, True),
    ("{a, c} == 12'hFF3", {"a": 255, "c": 3}, True),
    ("{2{c}} == 8'h55", {"c": 5}, True),
    ("a[3:0] == c", {"a": 0x35, "c": 5}, True),
    ("a[7 -: 4] == 4'h3", {"a": 0x35}, True),
    ("a[0 +: 4] == 4'h5", {"a": 0x35}, True),
    ("a[c] == 1", {"a": 0b1000, "c": 3}, True),
    ("s < t", {"s": 0xFF, "t": 1}, True),                # -1 < 1 signed
    ("a < b", {"a": 0xFF, "b": 1}, False),               # unsigned
    ("s < b", {"s": 0xFF, "b": 1}, False),               # mixed -> unsigned
    ("$signed(a) < 0", {"a": 0x80}, True),
    ("(s >>> 1) == 8'shC0", {"s": 0x80}, True),         # all signed: arithmetic shift
    ("(s >>> 1) == 8'hC0", {"s": 0x80}, False),          # unsigned operand: whole compare unsigned
    ("(a >> 1) == 8'h40", {"a": 0x80}, True),
    ("-a == 8'hFF", {"a": 1}, True),
    ("~a == 8'hF0", {"a": 0x0F}, True),
    ("&c", {"c": 15}, True), ("|c", {"c": 0}, False), ("^c", {"c": 7}, True),
    ("$onehot(a)", {"a": 8}, True), ("$onehot(a)", {"a": 0}, False), ("$onehot0(a)", {"a": 0}, True),
    ("$countones(a) == 3", {"a": 0b10101}, True),
    ("bit ? a == 1 : b == 1", {"bit": 1, "a": 1, "b": 0}, True),
    ("a / 3 == 5 && a % 3 == 1", {"a": 16}, True),
    ("a / 0 == 0", {"a": 16}, True),                     # X in SV; documented as 0
    ("a == DEPTH", {"a": 16}, True),
])
def test_expressions(expr, row, want):
    assert holds(expr, S8, row, params={"DEPTH": 16}) == want


def test_rose_fell_stable_past():
    sig = {"x": 1, "v": 4}
    rows = [{"x": 0, "v": 1}, {"x": 1, "v": 1}, {"x": 1, "v": 2}, {"x": 0, "v": 2}]
    r = run_trace({"rose": "$rose(x)", "fell": "$fell(x)", "stable": "$stable(v)",
                   "past": "v == $past(v) + 1", "past2": "v == $past(v, 2) + 1"},
                  sig, rows, initial={"x": 0, "v": 0})
    ticks = lambda n: [e.tick for e in r.events if e.assertion == n and e.kind == "pass"]
    assert ticks("rose") == [1]
    assert ticks("fell") == [3]
    assert ticks("stable") == [1, 3]
    assert ticks("past") == [0, 2]
    assert ticks("past2") == [0, 1, 2, 3]   # 1==0+1, 1==0+1 (initial), 2==1+1, 2==1+1


def test_past_without_history_is_unknown():
    r = run_trace({"st": "$stable(x)", "ch": "$changed(x)", "rose": "$rose(x)"},
                  {"x": 1}, [{"x": 1}])
    kinds = {e.assertion: e.kind for e in r.events}
    assert kinds == {"st": "fail", "ch": "pass", "rose": "pass"}


def test_past_with_gate():
    sig = {"en": 1, "v": 4}
    rows = [{"en": 1, "v": 1}, {"en": 0, "v": 2}, {"en": 0, "v": 3}, {"en": 1, "v": 4}, {"en": 1, "v": 5}]
    r = run_trace({"p": "$past(v, 1, en) == 1"}, sig, rows, initial={"en": 0, "v": 0})
    assert [e.tick for e in r.events if e.kind == "pass"] == [1, 2, 3]


def test_isunknown_uses_x_mask():
    sig = {"d": 4}
    rows = [{"d": 0, ("d", "x"): 0b0100}, {"d": 3}]
    r = run_trace({"p": "!$isunknown(d)", "n": "$countbits(d, 'x) == 1"}, sig, rows)
    assert sorted((e.assertion, e.tick, e.kind) for e in r.events) == [
        ("n", 0, "pass"), ("n", 1, "fail"), ("p", 0, "fail"), ("p", 1, "pass")]
