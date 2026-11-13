import pytest

from cocotbext.sva import _ast as A
from cocotbext.sva._errors import SvaSyntaxError
from cocotbext.sva._parser import parse_assertion, parse_expression


def prop(text):
    return parse_assertion(text).prop


def test_implication_and_delay_range():
    p = prop("req |-> ##[1:3] ack")
    assert isinstance(p, A.PImpl) and p.op == "|->"
    d = p.prop.seq
    assert isinstance(d, A.SDelay) and d.left is None and d.lo.value == 1 and d.hi.value == 3


def test_header_clock_and_disable():
    spec = parse_assertion("@(posedge clk iff en) disable iff (!rst_n) a |=> b")
    assert spec.clock.edge == "posedge" and spec.clock.signal.name == "clk"
    assert spec.clock.iff.name == "en"
    assert isinstance(spec.disable, A.Unary)


@pytest.mark.parametrize("text,kind", [
    ("a[*3]", ("*", 3, 3)), ("a[*1:$]", ("*", 1, None)), ("a[*]", ("*", 0, None)),
    ("a[+]", ("*", 1, None)), ("a[->2]", ("->", 2, 2)), ("a[=1:4]", ("=", 1, 4)),
])
def test_repetitions(text, kind):
    r = prop(text)
    assert r.kind == kind[0] and r.lo.value == kind[1]
    assert (None if r.hi_inf else r.hi.value) == kind[2]


def test_unbounded_delays():
    assert prop("a ##[*] b").hi_inf and prop("a ##[*] b").lo.value == 0
    assert prop("a ##[+] b").hi_inf and prop("a ##[+] b").lo.value == 1
    assert prop("a ##[2:$] b").hi_inf


def test_sequence_precedence():
    # throughout binds tighter than within, which binds tighter than intersect
    p = prop("x throughout s1 within s2 intersect s3")
    assert isinstance(p, A.SBin) and p.op == "intersect"
    assert p.left.op == "within" and isinstance(p.left.left, A.SThroughout)


def test_and_or_build_sequences_or_properties():
    assert isinstance(prop("a ##1 b and c"), A.SBin)
    assert isinstance(prop("(a |-> b) and c"), A.PBin)


def test_parenthesized_expression_vs_sequence():
    p = prop("(a || b) ##1 c")
    assert isinstance(p.left, A.SBool) and isinstance(p.left.expr, A.Binary)
    p = prop("(a ##1 b)[*2]")
    assert isinstance(p, A.SRepeat) and isinstance(p.seq, A.SDelay)


def test_match_items():
    p = prop("(req && ack, t = tag, u = tag + 1) |-> ##1 resp_tag == t")
    assert isinstance(p.seq, A.SMatchItems) and [n for n, _ in p.seq.assigns] == ["t", "u"]


def test_followed_by_if_case():
    assert prop("a #-# b").op == "#-#"
    p = prop("if (m) a |-> b else c")
    assert isinstance(p, A.PIf) and isinstance(p.then, A.PImpl)
    p = prop("case (st) 0, 1: a; default: b; endcase")
    assert isinstance(p, A.PCase) and len(p.items) == 2 and p.items[1][0] is None


def test_literals():
    e = parse_expression("8'hF_F")
    assert (e.value, e.width, e.signed) == (255, 8, False)
    e = parse_expression("4'sb10x1")
    assert (e.width, e.signed, e.xmask) == (4, True, 0b0010)
    e = parse_expression("'1")
    assert e.fill == "1"
    e = parse_expression("17")
    assert e.width is None and e.signed


def test_hierarchical_names_and_selects():
    e = parse_expression("u_ctrl.fifo.count[3:0] == data[7 -: 4]")
    assert e.left.base.path == ("u_ctrl", "fifo", "count")
    assert isinstance(e.right, A.PartSelect) and not e.right.up


def test_system_function_omitted_argument():
    e = parse_expression("$past(x,,en)")
    assert e.name == "$past" and e.args[1] is None and e.args[2].name == "en"


def test_syntax_error_has_caret():
    with pytest.raises(SvaSyntaxError) as exc:
        prop("a |-> ##[1:3 b")
    assert "^" in str(exc.value)
