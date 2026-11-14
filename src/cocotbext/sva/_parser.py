"""Recursive-descent parser for SVA assertions.

Operator precedence follows IEEE 1800-2017 tables 11-2 (expressions) and 16-3
(sequences and properties). Sequences and properties share one grammar: `and`
and `or` build sequence nodes when both operands are sequences, property nodes
otherwise. A parenthesized operand is first tried as a plain expression and
re-parsed as a sequence/property group if that fails (backtracking).
"""
from __future__ import annotations

from . import _ast as A
from ._errors import SvaSyntaxError
from ._lexer import Token, tokenize

_PREFIX_PROPS = {"always", "s_always", "eventually", "s_eventually", "if", "case",
                 "accept_on", "reject_on", "sync_accept_on", "sync_reject_on"}

# Expression binary operators: precedence (higher binds tighter), right-assoc flag.
_BINOPS = {
    "**": (12, False),
    "*": (11, False), "/": (11, False), "%": (11, False),
    "+": (10, False), "-": (10, False),
    "<<": (9, False), ">>": (9, False), "<<<": (9, False), ">>>": (9, False),
    "<": (8, False), "<=": (8, False), ">": (8, False), ">=": (8, False),
    "==": (7, False), "!=": (7, False), "===": (7, False), "!==": (7, False),
    "==?": (7, False), "!=?": (7, False),
    "&": (6, False),
    "^": (5, False), "~^": (5, False), "^~": (5, False),
    "|": (4, False),
    "&&": (3, False),
    "||": (2, False),
}
_UNOPS = {"+", "-", "!", "~", "&", "~&", "|", "~|", "^", "~^", "^~"}


class _Backtrack(Exception):
    pass


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    # -- token helpers ------------------------------------------------------
    @property
    def tok(self) -> Token:
        return self.toks[self.i]

    def peek(self, k=1) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def at(self, text: str, kind: str | None = None) -> bool:
        t = self.tok
        return t.text == text and (kind is None or t.kind == kind) and t.kind in ("op", "kw")

    def accept(self, text: str) -> Token | None:
        if self.at(text):
            t = self.tok
            self.i += 1
            return t
        return None

    def expect(self, text: str) -> Token:
        if not self.at(text):
            self.error(f"expected {text!r}, found {self.tok.text or 'end of text'!r}")
        t = self.tok
        self.i += 1
        return t

    def error(self, msg: str, tok: Token | None = None):
        tok = tok or self.tok
        raise SvaSyntaxError(msg, self.text, tok.pos, tok.pos + max(1, len(tok.text)))

    def span_from(self, start_pos: int) -> tuple[int, int]:
        prev = self.toks[self.i - 1] if self.i > 0 else self.tok
        return (start_pos, prev.pos + len(prev.text))

    # -- entry points -------------------------------------------------------
    def parse_assertion(self) -> A.AssertionSpec:
        start = self.tok.pos
        clock = None
        disable = None
        if self.at("@"):
            clock = self.parse_clock_event()
        if self.at("disable"):
            self.i += 1
            self.expect("iff")
            self.expect("(")
            disable = self.parse_expr()
            self.expect(")")
        prop = self.parse_prop()
        if self.tok.kind != "eof":
            self.error(f"unexpected {self.tok.text!r}")
        return A.AssertionSpec(clock, disable, prop, span=self.span_from(start))

    def parse_clock_event(self) -> A.ClockEvent:
        start = self.expect("@").pos
        self.expect("(")
        edge = None
        if self.tok.kind == "kw" and self.tok.text in ("posedge", "negedge", "edge"):
            edge = self.tok.text
            self.i += 1
        sig = self.parse_expr()
        iff = None
        if self.accept("iff"):
            iff = self.parse_expr()
        self.expect(")")
        return A.ClockEvent(edge, sig, iff, span=self.span_from(start))

    def parse_only_expr(self) -> A.Node:
        e = self.parse_expr()
        if self.tok.kind != "eof":
            self.error(f"unexpected {self.tok.text!r}")
        return e

    def parse_only_seq(self) -> A.Node:
        s = self.parse_prop()
        if self.tok.kind != "eof":
            self.error(f"unexpected {self.tok.text!r}")
        return s

    # -- properties (lowest precedence first) -------------------------------
    def parse_prop(self) -> A.Node:
        t = self.tok
        if t.kind == "kw" and t.text in _PREFIX_PROPS:
            return self.parse_prefix_prop()
        return self.parse_impl()

    def parse_prefix_prop(self) -> A.Node:
        t = self.tok
        start = t.pos
        kw = t.text
        self.i += 1
        if kw in ("always", "s_always", "eventually", "s_eventually"):
            lo = hi = None
            hi_inf = False
            if self.at("["):
                lo, hi, hi_inf = self.parse_range_brackets("[")
            p = self.parse_prop()
            return A.PUnaryTemporal(kw, p, lo, hi, hi_inf, span=self.span_from(start))
        if kw == "if":
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then = self.parse_prop()
            other = None
            if self.accept("else"):
                other = self.parse_prop()
            return A.PIf(cond, then, other, span=self.span_from(start))
        if kw == "case":
            self.expect("(")
            sel = self.parse_expr()
            self.expect(")")
            items = []
            while not self.accept("endcase"):
                if self.accept("default"):
                    self.accept(":")
                    vals = None
                else:
                    vals = [self.parse_expr()]
                    while self.accept(","):
                        vals.append(self.parse_expr())
                    self.expect(":")
                p = self.parse_prop()
                self.expect(";")
                items.append((vals, p))
            return A.PCase(sel, items, span=self.span_from(start))
        # accept_on / reject_on / sync_*
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        p = self.parse_prop()
        return A.PAbort(kw, cond, p, span=self.span_from(start))

    def _rhs_prop(self, sub):
        t = self.tok
        if t.kind == "kw" and t.text in _PREFIX_PROPS:
            return self.parse_prefix_prop()
        return sub()

    def parse_impl(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_until()
        for op in ("|->", "|=>", "#-#", "#=#"):
            if self.at(op):
                optok = self.tok
                self.i += 1
                if not A.is_seq(left) and not A.is_expr(left):
                    self.error(f"left operand of {op} must be a sequence", optok)
                right = self._rhs_prop(self.parse_impl)
                return A.PImpl(op, _as_seq(left), _as_prop(right), span=self.span_from(start))
        return left

    def parse_until(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_iff()
        for op in ("until", "s_until", "until_with", "s_until_with", "implies"):
            if self.at(op):
                self.i += 1
                right = self._rhs_prop(self.parse_until)
                return A.PBin(op, _as_prop(left), _as_prop(right), span=self.span_from(start))
        return left

    def parse_iff(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_or()
        if self.accept("iff"):
            right = self._rhs_prop(self.parse_iff)
            return A.PBin("iff", _as_prop(left), _as_prop(right), span=self.span_from(start))
        return left

    def parse_or(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_and()
        while self.accept("or"):
            right = self._rhs_prop(self.parse_and)
            left = _seq_or_prop("or", left, right, self.span_from(start))
        return left

    def parse_and(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_not()
        while self.accept("and"):
            right = self._rhs_prop(self.parse_not)
            left = _seq_or_prop("and", left, right, self.span_from(start))
        return left

    def parse_not(self) -> A.Node:
        t = self.tok
        start = t.pos
        if self.accept("not"):
            p = self._rhs_prop(self.parse_not)
            return A.PNot(_as_prop(p), span=self.span_from(start))
        if t.kind == "kw" and t.text in ("nexttime", "s_nexttime"):
            self.i += 1
            lo = None
            if self.accept("["):
                lo = self.parse_expr()
                self.expect("]")
            p = self._rhs_prop(self.parse_not)
            return A.PUnaryTemporal(t.text, _as_prop(p), lo, lo, span=self.span_from(start))
        return self.parse_intersect()

    # -- sequences ----------------------------------------------------------
    def parse_intersect(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_within()
        while self.accept("intersect"):
            right = self.parse_within()
            left = A.SBin("intersect", _as_seq(left), _as_seq(right), span=self.span_from(start))
        return left

    def parse_within(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_throughout()
        while self.accept("within"):
            right = self.parse_throughout()
            left = A.SBin("within", _as_seq(left), _as_seq(right), span=self.span_from(start))
        return left

    def parse_throughout(self) -> A.Node:
        start = self.tok.pos
        left = self.parse_delay()
        if self.at("throughout"):
            optok = self.tok
            self.i += 1
            if not A.is_expr(left):
                self.error("left operand of throughout must be a boolean expression", optok)
            right = self.parse_throughout()
            return A.SThroughout(left, _as_seq(right), span=self.span_from(start))
        return left

    def parse_delay(self) -> A.Node:
        start = self.tok.pos
        if self.at("##"):
            left = None
        else:
            left = self.parse_repeat()
        while self.at("##"):
            self.i += 1
            lo, hi, hi_inf = self.parse_delay_range()
            right = self.parse_repeat()
            left = A.SDelay(None if left is None else _as_seq(left), lo, hi, _as_seq(right),
                            hi_inf, span=self.span_from(start))
        return left

    def parse_delay_range(self):
        t = self.tok
        if t.kind == "num":
            self.i += 1
            n = _num_node(t)
            return n, n, False
        if t.kind == "id":
            self.i += 1
            n = A.Id((t.text,), span=(t.pos, t.pos + len(t.text)))
            return n, n, False
        if self.at("("):
            self.i += 1
            n = self.parse_expr()
            self.expect(")")
            return n, n, False
        if self.at("[*"):           # ##[*]
            self.i += 1
            self.expect("]")
            return A.Num(0, None, True), None, True
        if self.at("[+]"):
            self.i += 1
            return A.Num(1, None, True), None, True
        if self.at("["):
            return self.parse_range_brackets("[")
        self.error("expected a delay after ##")

    def parse_range_brackets(self, opener: str):
        """Parse `[lo:hi]`, `[lo:$]` or `[n]` after the opener token."""
        self.expect(opener)
        lo = self.parse_expr()
        hi = lo
        hi_inf = False
        if self.accept(":"):
            if self.accept("$"):
                hi = None
                hi_inf = True
            else:
                hi = self.parse_expr()
        self.expect("]")
        return lo, hi, hi_inf

    def parse_repeat(self) -> A.Node:
        start = self.tok.pos
        node = self.parse_seq_primary()
        while True:
            if self.at("[*"):
                self.i += 1
                if self.accept("]"):     # [*]
                    node = A.SRepeat(_as_seq(node), "*", A.Num(0, None, True), None, True,
                                     span=self.span_from(start))
                    continue
                self.i -= 1
                lo, hi, hi_inf = self.parse_range_brackets("[*")
                node = A.SRepeat(_as_seq(node), "*", lo, hi, hi_inf, span=self.span_from(start))
            elif self.at("[+]"):
                self.i += 1
                node = A.SRepeat(_as_seq(node), "*", A.Num(1, None, True), None, True,
                                 span=self.span_from(start))
            elif self.at("[->") or self.at("[="):
                kind = "->" if self.tok.text == "[->" else "="
                optok = self.tok
                if not A.is_expr(node):
                    self.error(f"[{kind} repetition needs a boolean expression operand", optok)
                lo, hi, hi_inf = self.parse_range_brackets(optok.text)
                node = A.SRepeat(node, kind, lo, hi, hi_inf, span=self.span_from(start))
            else:
                return node

    def parse_seq_primary(self) -> A.Node:
        t = self.tok
        start = t.pos
        if t.kind == "kw" and t.text == "first_match":
            self.i += 1
            self.expect("(")
            s = self.parse_prop()
            node = A.SFirstMatch(_as_seq(s), span=self.span_from(start))
            if self.accept(","):
                node = A.SMatchItems(node, self.parse_match_items(), span=self.span_from(start))
            self.expect(")")
            node.span = self.span_from(start)
            return node
        if t.kind == "kw" and t.text in ("strong", "weak"):
            self.i += 1
            self.expect("(")
            s = self.parse_prop()
            self.expect(")")
            return A.PSeq(_as_seq(s), t.text, span=self.span_from(start))
        if self.at("("):
            save = self.i
            try:
                return self._try_expr()
            except (SvaSyntaxError, _Backtrack):
                self.i = save
            self.i += 1
            inner = self.parse_prop()
            if self.accept(","):
                inner = A.SMatchItems(_as_seq(inner), self.parse_match_items(),
                                      span=self.span_from(start))
            self.expect(")")
            if isinstance(inner, A.Node) and A.is_expr(inner):
                # "(expr)" that the expression parser rejected only because of
                # what follows; keep it as an expression.
                return inner
            return inner
        return self.parse_expr()

    def _try_expr(self) -> A.Node:
        e = self.parse_expr()
        # An expression in parentheses followed by sequence operators is fine;
        # but if parsing stopped inside the parentheses (e.g. at ##), the
        # expression parser has already raised.
        return e

    def parse_match_items(self) -> list:
        items = []
        while True:
            t = self.tok
            if t.kind != "id":
                self.error("expected a local variable assignment (name = expression)")
            self.i += 1
            self.expect("=")
            items.append((t.text, self.parse_expr()))
            if not self.accept(","):
                return items

    # -- expressions --------------------------------------------------------
    def parse_expr(self) -> A.Node:
        """Expression; `->` / `<->` (logical implication / equivalence, lowest
        precedence, right associative) are rewritten to && / || / !."""
        start = self.tok.pos
        left = self.parse_cond()
        t = self.tok
        if t.kind == "op" and t.text in ("->", "<->"):
            self.i += 1
            right = self.parse_expr()
            span = self.span_from(start)
            if t.text == "->":
                return A.Binary("||", A.Unary("!", left, span=left.span), right, span=span)
            return A.Binary("==", A.Unary("!", left, span=left.span),
                            A.Unary("!", right, span=right.span), span=span)
        return left

    def parse_cond(self) -> A.Node:
        start = self.tok.pos
        c = self.parse_binary(1)
        if self.accept("?"):
            a = self.parse_cond()
            self.expect(":")
            b = self.parse_cond()
            return A.Cond(c, a, b, span=self.span_from(start))
        return c

    def parse_binary(self, min_prec: int) -> A.Node:
        start = self.tok.pos
        left = self.parse_unary()
        while True:
            t = self.tok
            if t.kind != "op" or t.text not in _BINOPS:
                return left
            prec, right_assoc = _BINOPS[t.text]
            if prec < min_prec:
                return left
            self.i += 1
            right = self.parse_binary(prec if right_assoc else prec + 1)
            left = A.Binary(t.text, left, right, span=self.span_from(start))

    def parse_unary(self) -> A.Node:
        t = self.tok
        if t.kind == "op" and t.text in _UNOPS:
            self.i += 1
            operand = self.parse_unary()
            return A.Unary(t.text, operand, span=self.span_from(t.pos))
        return self.parse_postfix()

    def parse_postfix(self) -> A.Node:
        start = self.tok.pos
        node = self.parse_primary()
        while True:
            if self.at("["):
                self.i += 1
                a = self.parse_expr()
                if self.accept(":"):
                    b = self.parse_expr()
                    self.expect("]")
                    node = A.Slice(node, a, b, span=self.span_from(start))
                elif self.at("+:") or self.at("-:"):
                    up = self.tok.text == "+:"
                    self.i += 1
                    w = self.parse_expr()
                    self.expect("]")
                    node = A.PartSelect(node, a, w, up, span=self.span_from(start))
                else:
                    self.expect("]")
                    node = A.Index(node, a, span=self.span_from(start))
            elif self.at(".") and self.peek().kind == "id" and isinstance(node, A.Id):
                self.i += 1
                node = A.Id(node.path + (self.tok.text,), span=self.span_from(start))
                self.i += 1
            else:
                return node

    def parse_primary(self) -> A.Node:
        t = self.tok
        start = t.pos
        if t.kind == "num":
            self.i += 1
            return _num_node(t)
        if t.kind == "id":
            self.i += 1
            if self.at("("):
                self.i += 1
                args = self.parse_call_args(allow_seq=True)
                return A.Call(t.text, args, span=self.span_from(start))
            return A.Id((t.text,), span=self.span_from(start))
        if t.kind == "sys":
            self.i += 1
            args = []
            if self.accept("("):
                args = self.parse_call_args(allow_seq=False)
            return A.Call(t.text, args, span=self.span_from(start))
        if self.accept("("):
            e = self.parse_expr()
            if not self.at(")"):
                raise _Backtrack()
            self.i += 1
            e.span = self.span_from(start)
            return e
        if self.accept("{"):
            first = self.parse_expr()
            if self.at("{"):                     # replication {n{...}}
                self.i += 1
                items = [self.parse_expr()]
                while self.accept(","):
                    items.append(self.parse_expr())
                self.expect("}")
                self.expect("}")
                return A.Repl(first, items, span=self.span_from(start))
            items = [first]
            while self.accept(","):
                items.append(self.parse_expr())
            self.expect("}")
            return A.Concat(items, span=self.span_from(start))
        if t.kind == "eof":
            self.error("unexpected end of text")
        self.error(f"unexpected {t.text!r}")

    def parse_call_args(self, allow_seq: bool) -> list:
        args = []
        if self.accept(")"):
            return args
        while True:
            if self.at(",") or self.at(")"):
                args.append(None)            # omitted argument, e.g. $past(x,,en)
            elif self.at("@"):
                args.append(self.parse_clock_event())
            else:
                # user-defined sequence/property instances may take sequence
                # arguments; system-function arguments are expressions
                args.append(self.parse_prop() if allow_seq else self.parse_expr())
            if self.accept(")"):
                return args
            self.expect(",")


def _num_node(t: Token) -> A.Num:
    v = t.value
    span = (t.pos, t.pos + len(t.text))
    if v[0] == "fill":
        return A.Num(0, None, False, xmask=0, fill=v[1], span=span)
    value, width, signed, xmask = v
    if width is None and "'" in t.text:
        # unsized based literal: 32-bit, unsigned unless 's
        return A.Num(value, None, signed, xmask, span=span)
    return A.Num(value, width, signed if width is not None else True, xmask, span=span)


def _as_seq(node: A.Node) -> A.Node:
    if A.is_expr(node):
        return A.SBool(node, span=node.span)
    return node


def _as_prop(node: A.Node) -> A.Node:
    if A.is_expr(node) or A.is_seq(node):
        return A.PSeq(_as_seq(node), span=node.span)
    return node


def _seq_or_prop(op: str, left: A.Node, right: A.Node, span) -> A.Node:
    left_seq = A.is_seq(left) or A.is_expr(left)
    right_seq = A.is_seq(right) or A.is_expr(right)
    if left_seq and right_seq:
        return A.SBin(op, _as_seq(left), _as_seq(right), span=span)
    return A.PBin(op, _as_prop(left), _as_prop(right), span=span)


def parse_assertion(text: str) -> A.AssertionSpec:
    return Parser(text).parse_assertion()


def parse_expression(text: str) -> A.Node:
    return Parser(text).parse_only_expr()


def parse_sequence_or_property(text: str) -> A.Node:
    return Parser(text).parse_only_seq()
