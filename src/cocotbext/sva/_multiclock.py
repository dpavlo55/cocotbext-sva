"""Multi-clock properties, rewritten to single-clock ones.

A property that uses several clocks is evaluated on the *union* of their
ticks: one tick of the engine per time step in which at least one of the
clocks ticks. For every clock c there is a boolean `t_c`, true at the union
ticks where c ticks. The property is rewritten over that tick stream with the
rules of IEEE 1800-2017 F.5 (rewriting of clocked sequences and properties),
so the engine evaluates it like any single-clock property:

    boolean b under clock c          (!t_c)[*0:$] ##1 (t_c && b)
    R1 ##1 R2, R1 ##0 R2             the same operator on the rewritten operands
    R1 ##n R2 (same clock)           R1 ##1 (one tick of c)[*n-1] ##1 R2
    R |-> P, R |=> P                 the same operator: |=> moves to the next
                                     union tick, and P waits there for a tick
                                     of its own clock
    nexttime, always, eventually,    the same operator, counting only the
    until (and their s_ forms)       ticks at which t_c is true
    if, not, aborts, ...             evaluated from the first tick of their
                                     clock on (PAlign)
    $past, $rose, ... under c        gated by t_c

Between differently clocked sequences only ##1 and ##0 are allowed (16.13.1):
`@(c1) s1 ##1 @(c2) s2` continues at the first tick of c2 strictly after the
end of s1, `##0` (and |->) at the first one that is not before it.

Attempts start at the ticks of the leading clock only; the engine does that
(GatedStep), not the rewritten property.
"""
from __future__ import annotations

import copy

from . import _ast as A
from ._errors import SvaError, SvaUnsupportedError

_SAMPLED_1 = ("$rose", "$fell", "$stable", "$changed")


def has_clocks(node) -> bool:
    """Does the property contain clocking events of its own?"""
    return any(isinstance(n, A.Clocked) or
               (isinstance(n, A.Call) and any(isinstance(a, A.ClockEvent) for a in n.args))
               for n in _walk(node))


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if not isinstance(n, A.Node):
            continue
        yield n
        for v in vars(n).values():
            if isinstance(v, A.Node):
                stack.append(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, A.Node):
                        stack.append(item)
                    elif isinstance(item, tuple):
                        for x in item:
                            if isinstance(x, A.Node):
                                stack.append(x)
                            elif isinstance(x, list):
                                stack += [y for y in x if isinstance(y, A.Node)]


def _num(n: int) -> A.Num:
    return A.Num(n, None, True)


class _EndClocks(SvaError):
    """The operands of a sequence `and` / `or` end on different clocks."""


class Rewriter:
    """rewrite(prop) -> (prop over union ticks, id of the leading clock).

    clock_id(event) -> hashable id of a clocking event (A.ClockEvent)
    tick(id)        -> expression that is true at the union ticks of clock id
    const(node)     -> value of a constant expression
    named           -> named sequences / properties (NamedDef), expanded here
    kind            -> 'assert' | 'assume' | 'cover' (default strength)
    """

    def __init__(self, clock_id, tick, const, named=None, kind="assert", text=""):
        self.clock_id = clock_id
        self.tick = tick
        self.const = const
        self.named = named or {}
        self.kind = kind
        self.text = text
        self.used: set = set()

    # -- entry points ---------------------------------------------------------
    def analyse(self, prop, default):
        """(leading clock id, set of clock ids used) without rewriting."""
        self.used = set()
        lead = self._lead(prop, default)
        self._collect(prop, default)
        if len(lead) != 1:
            raise SvaError("a multi-clock property must have a single leading clock "
                           "(the clock of the first sequence of each operand)")
        return next(iter(lead)), set(self.used)

    def rewrite(self, prop, default):
        lead, _ = self.analyse(prop, default)
        strength = "strong" if self.kind == "cover" else "weak"
        return self.P(prop, default, lead, strength), lead

    # -- helpers --------------------------------------------------------------
    def err(self, msg, node, cls=SvaError):
        if self.text and node is not None and node.span != (0, 0):
            s, e = node.span
            msg = f"{msg}\n    {self.text}\n    {' ' * s}{'^' * max(1, e - s)}"
        raise cls(msg)

    def _use(self, c):
        self.used.add(c)
        return c

    def one_tick(self, c) -> A.Node:
        """Sequence: up to and including the next tick of c (`1` under c)."""
        t = self.tick(c)
        return A.SDelay(A.SRepeat(A.SBool(A.Unary("!", t)), "*", _num(0), None, True),
                        _num(1), _num(1), A.SBool(t))

    def ticks(self, c, lo: int, hi) -> A.Node:
        """Sequence matching at the lo-th .. hi-th tick of c from now (1-based;
        hi None: unbounded)."""
        return A.SRepeat(self.one_tick(c), "*", _num(lo), None if hi is None else _num(hi), hi is None)

    def _named(self, expr):
        if isinstance(expr, A.Id) and len(expr.path) == 1 and expr.path[0] in self.named:
            d = self.named[expr.path[0]]
            if d.args:
                self.err(f"'{d.name}' needs {len(d.args)} argument(s)", expr)
            return d.ast
        if isinstance(expr, A.Call) and expr.name in self.named:
            from ._compile import substitute
            d = self.named[expr.name]
            if len(expr.args) != len(d.args):
                self.err(f"'{d.name}' takes {len(d.args)} argument(s)", expr)
            return substitute(d.ast, dict(zip(d.args, expr.args)))
        return None

    def _range(self, node):
        lo = self.const(node.lo)
        hi = None if node.hi_inf else self.const(node.hi)
        if lo < 0 or (hi is not None and hi < lo):
            self.err("range must have 0 <= lo <= hi", node)
        return lo, hi

    # -- analysis: leading clocks and clocks used -----------------------------
    def _cid(self, n: A.Clocked, c):
        return c if n.clock is None else self.clock_id(n.clock)

    def _lead(self, n, c) -> set:
        if isinstance(n, A.Clocked):
            return self._lead(n.body, self._cid(n, c))
        if A.is_expr(n):
            sub = self._named(n)
            return self._lead(sub, c) if sub is not None else {c}
        if isinstance(n, A.SBool):
            return self._lead(n.expr, c)
        if isinstance(n, A.SDelay):
            return self._lead(n.left, c) if n.left is not None else {c}
        if isinstance(n, (A.SRepeat, A.SFirstMatch, A.SMatchItems, A.PSeq)):
            return self._lead(n.seq, c)
        if isinstance(n, A.SThroughout):
            return {c} | self._lead(n.seq, c)
        if isinstance(n, A.SBin) or (isinstance(n, A.PBin) and n.op in ("and", "or", "iff", "implies")):
            return self._lead(n.left, c) | self._lead(n.right, c)
        if isinstance(n, A.PImpl):
            return self._lead(n.seq, c)
        if isinstance(n, A.PNot):
            return self._lead(n.prop, c)
        if isinstance(n, A.PBin) and n.op not in ("and", "or", "iff", "implies"):
            return {c}
        # if, case, aborts and the temporal operators sample or count on c
        return {c}

    def _collect(self, n, c):
        """Record every clock that clocks something (self.used)."""
        if isinstance(n, A.Clocked):
            self._collect(n.body, self._cid(n, c))
            return
        if A.is_expr(n):
            sub = self._named(n)
            if sub is not None:
                self._collect(sub, c)
                return
            self._use(c)
            for x in _walk(n):
                if isinstance(x, A.Call):
                    for a in x.args:
                        if isinstance(a, A.ClockEvent):
                            self._use(self.clock_id(a))
            return
        if not isinstance(n, A.Node):
            return
        if isinstance(n, (A.PIf, A.PCase, A.PUnaryTemporal, A.PAbort, A.SDelay)):
            self._use(c)
        for v in vars(n).values():
            if isinstance(v, A.Node):
                self._collect(v, c)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, A.Node):
                        self._collect(item, c)
                    elif isinstance(item, tuple):
                        for x in item:
                            if isinstance(x, A.Node):
                                self._collect(x, c)

    # -- expressions ----------------------------------------------------------
    def E(self, e, c):
        """Expression under clock c: sampled-value functions follow c's ticks."""
        if not isinstance(e, A.Node) or isinstance(e, A.ClockEvent):
            return e
        if isinstance(e, A.Call) and e.name == "$past":
            args = list(e.args) + [None] * (4 - len(e.args))
            cc = self._use(self.clock_id(args[3])) if isinstance(args[3], A.ClockEvent) else c
            gate = self.tick(cc)
            if args[2] is not None:
                gate = A.Binary("&&", gate, self.E(args[2], cc))
            return A.Call("$past", [self.E(args[0], cc), args[1], gate], span=e.span)
        if isinstance(e, A.Call) and e.name in _SAMPLED_1:
            ev = e.args[1] if len(e.args) > 1 else None
            cc = self._use(self.clock_id(ev)) if isinstance(ev, A.ClockEvent) else c
            return A.Call(e.name, [self.E(e.args[0], cc), self.tick(cc)], span=e.span)
        new = copy.copy(e)
        for k, v in vars(e).items():
            if isinstance(v, A.Node):
                setattr(new, k, self.E(v, c))
            elif isinstance(v, list):
                setattr(new, k, [self.E(x, c) for x in v])
        return new

    # -- sequences ------------------------------------------------------------
    def S(self, n, c):
        """(sequence over union ticks, clock at its end)."""
        if isinstance(n, A.Clocked):
            return self.S(n.body, self._cid(n, c))
        if A.is_expr(n):
            n = A.SBool(n, span=n.span)
        if isinstance(n, A.SBool):
            sub = self._named(n.expr)
            if sub is not None:
                return self.S(sub, c)
            self._use(c)
            t = self.tick(c)
            if isinstance(n.expr, A.Num) and n.expr.value == 1 and not n.expr.xmask and n.expr.fill is None:
                return self.one_tick(c), c
            b = A.SBool(A.Binary("&&", t, self.E(n.expr, c), span=n.span), span=n.span)
            return A.SDelay(A.SRepeat(A.SBool(A.Unary("!", t)), "*", _num(0), None, True),
                            _num(1), _num(1), b, span=n.span), c
        if isinstance(n, A.SDelay):
            return self._delay(n, c)
        if isinstance(n, A.SRepeat):
            if n.kind == "*":
                body, end = self.S(n.seq, c)
                if end != c and (n.hi_inf or self.const(n.hi) > 1):
                    self.err("a repeated sequence must end on the clock it starts on", n)
                return A.SRepeat(body, "*", n.lo, n.hi, n.hi_inf, span=n.span), end
            # b[->n] = ((!b)[*0:$] ##1 b)[*n] ;  b[=n] = b[->n] ##1 (!b)[*0:$]
            expr = n.seq.expr if isinstance(n.seq, A.SBool) else n.seq
            notb = A.SBool(A.Unary("!", expr, span=expr.span), span=expr.span)
            skip = A.SRepeat(notb, "*", _num(0), None, True)
            one = A.SDelay(skip, _num(1), _num(1), A.SBool(expr, span=expr.span))
            seq = A.SRepeat(one, "*", n.lo, n.hi, n.hi_inf, span=n.span)
            if n.kind == "=":
                seq = A.SDelay(seq, _num(1), _num(1), skip, span=n.span)
            return self.S(seq, c)
        if isinstance(n, A.SBin):
            if n.op == "within":
                anyt = A.SRepeat(A.SBool(_num(1)), "*", _num(0), None, True)
                outer = A.SDelay(A.SDelay(anyt, _num(1), _num(1), n.left), _num(1), _num(1), anyt)
                return self.S(A.SBin("intersect", outer, n.right, span=n.span), c)
            left, e1 = self.S(n.left, c)
            right, e2 = self.S(n.right, c)
            if e1 != e2:
                self.err(f"the operands of the sequence operator '{n.op}' must end on the same clock",
                         n, _EndClocks)
            return A.SBin(n.op, left, right, span=n.span), e1
        if isinstance(n, A.SThroughout):
            hold = A.SRepeat(A.SBool(n.expr, span=n.expr.span), "*", _num(0), None, True)
            return self.S(A.SBin("intersect", hold, n.seq, span=n.span), c)
        if isinstance(n, A.SFirstMatch):
            body, end = self.S(n.seq, c)
            return A.SFirstMatch(body, span=n.span), end
        if isinstance(n, A.SMatchItems):
            body, end = self.S(n.seq, c)
            return A.SMatchItems(body, [(name, self.E(x, end)) for name, x in n.assigns],
                                 span=n.span), end
        if isinstance(n, A.PSeq) and n.strength:
            self.err(f"{n.strength}() is a property, not a sequence", n)
        self.err("expected a sequence", n)

    def _delay(self, n: A.SDelay, c):
        lo, hi = self._range(n)
        self._use(c)
        # clock of the right operand: its own, else the enclosing one (a clock
        # set inside a parenthesized left operand does not flow out of it)
        rc = self._cid(n.right, c) if isinstance(n.right, A.Clocked) else c
        if n.left is None:
            # ##n S = 1 ##n S ; ##0 S = S
            if rc != c:
                self.err("a leading delay cannot be followed by a sequence on another clock", n)
            if lo == 0:
                first = self.S(n.right, c)
                if hi == 0:
                    return first
                rest = self._delay(A.SDelay(A.SBool(_num(1)), _num(1), None if hi is None else _num(hi),
                                            n.right, hi is None, span=n.span), c)
                return self._or(first, rest, n)
            return self._delay(A.SDelay(A.SBool(_num(1)), n.lo, n.hi, n.right, n.hi_inf, span=n.span), c)
        left, lend = self.S(n.left, c)
        if rc != lend:
            # the clock changes at this delay
            if (lo, hi) not in ((0, 0), (1, 1)):
                self.err("only ##1 and ##0 are allowed between differently clocked sequences", n)
            if self._nullable(n.left) or self._nullable(n.right):
                self.err("a sequence next to a clock change must not admit an empty match (16.13.1)", n)
            right, rend = self.S(n.right, rc)
            return A.SDelay(left, _num(lo), _num(lo), right, span=n.span), rend
        right, rend = self.S(n.right, c)
        if lo == 0:
            fused = (A.SDelay(left, _num(0), _num(0), right, span=n.span), rend)
            if hi == 0:
                return fused
            later = self._gap(left, 1, hi, right, c, n)
            return self._or(fused, (later, rend), n)
        return self._gap(left, lo, hi, right, c, n), rend

    def _nullable(self, n) -> bool:
        """Can the sequence match without consuming a tick?"""
        if isinstance(n, A.Clocked):
            return self._nullable(n.body)
        if A.is_expr(n) or isinstance(n, A.SBool):
            sub = self._named(n.expr if isinstance(n, A.SBool) else n)
            return sub is not None and self._nullable(sub)
        if isinstance(n, A.SDelay):
            if n.left is None:
                return self.const(n.lo) == 0 and self._nullable(n.right)
            if self.const(n.lo) == 0:
                return False            # a fusion needs a tick (or a later alternative: lo >= 1)
            return self._nullable(n.left) and self._nullable(n.right) and self.const(n.lo) == 1
        if isinstance(n, A.SRepeat):
            return self.const(n.lo) == 0 or (n.kind == "*" and self._nullable(n.seq))
        if isinstance(n, A.SBin):
            if n.op == "or":
                return self._nullable(n.left) or self._nullable(n.right)
            return self._nullable(n.left) and self._nullable(n.right)
        if isinstance(n, (A.SFirstMatch, A.SMatchItems, A.SThroughout)):
            return self._nullable(n.seq)
        return False

    def _gap(self, left, lo, hi, right, c, n):
        """left ##[lo:hi] right on clock c, lo >= 1 (operands already rewritten)."""
        seq = left
        if not (lo == 1 and hi == 1):
            seq = A.SDelay(seq, _num(1), _num(1), self.ticks(c, lo - 1, None if hi is None else hi - 1))
        return A.SDelay(seq, _num(1), _num(1), right, span=n.span)

    def _or(self, a, b, n):
        if a[1] != b[1]:
            self.err("the alternatives of this delay end on different clocks", n)
        return A.SBin("or", a[0], b[0], span=n.span), a[1]

    # -- properties -----------------------------------------------------------
    def P(self, n, c, at, strength: str):
        """Property under clock c, over union ticks. at: the clock at a tick
        of which the evaluation is known to start (None: any union tick).
        strength: default strength of sequences."""
        if isinstance(n, A.Clocked):
            return self.P(n.body, self._cid(n, c), at, strength)
        if A.is_expr(n) or A.is_seq(n):
            n = A.PSeq(n if A.is_seq(n) else A.SBool(n, span=n.span), span=n.span)
        if isinstance(n, A.PSeq):
            if isinstance(n.seq, A.SBool) or A.is_expr(n.seq):
                sub = self._named(n.seq.expr if isinstance(n.seq, A.SBool) else n.seq)
                if sub is not None and not (A.is_seq(sub) or A.is_expr(sub)):
                    return self.P(sub, c, at, n.strength or strength)   # a named property
            try:
                # a sequence waits for the first tick of its clock by itself
                seq, _ = self.S(n.seq, c)
            except _EndClocks:
                # differently clocked operands: `and` / `or` of properties
                inner = n.seq
                while isinstance(inner, A.Clocked):
                    c = self._cid(inner, c)
                    inner = inner.body
                if not (isinstance(inner, A.SBin) and inner.op in ("and", "or")):
                    raise
                st = n.strength or strength
                return A.PBin(inner.op, self.P(A.PSeq(inner.left, n.strength), c, at, st),
                              self.P(A.PSeq(inner.right, n.strength), c, at, st), span=n.span)
            return A.PSeq(seq, n.strength or strength, span=n.span)
        # Every other operator is evaluated from the first tick of its leading
        # clock on. Until then it is unstarted, like the consequent of |=>
        # before its first tick (PAlign).
        leads = self._lead(n, c)
        if len(leads) == 1:
            lead = next(iter(leads))
            if at != lead:
                self._use(lead)
                return A.PAlign(self.tick(lead), self.P(n, c, lead, strength), span=n.span)
        if isinstance(n, A.PNot):
            return A.PNot(self.P(n.prop, c, at, strength), span=n.span)
        if isinstance(n, A.PImpl):
            ante, end = self.S(n.seq, c)
            # the consequent is clocked like the end of the antecedent
            # (16.13.3), unless that clock was set inside parentheses
            flow = c if isinstance(n.seq, A.Clocked) and n.seq.clock is None else end
            cons = self.P(n.prop, flow, end if n.op in ("|->", "#-#") else None, strength)
            return A.PImpl(n.op, ante, cons, span=n.span)
        if isinstance(n, A.PBin) and n.op in ("and", "or", "iff", "implies"):
            return A.PBin(n.op, self.P(n.left, c, at, strength),
                          self.P(n.right, c, at, strength), span=n.span)
        # the remaining operators sample or count on c itself (their leading clock)
        self._use(c)
        t = self.tick(c)
        if isinstance(n, A.PBin):
            st = "strong" if n.op.startswith("s_") else strength
            return A.PBin(n.op, self.P(n.left, c, c, st), self.P(n.right, c, c, st), tick=t, span=n.span)
        if isinstance(n, A.PIf):
            return A.PIf(self.E(n.cond, c), self.P(n.then, c, c, strength),
                         None if n.other is None else self.P(n.other, c, c, strength), span=n.span)
        if isinstance(n, A.PCase):
            return self.P(_case_to_if(n, self), c, at, strength)
        if isinstance(n, A.PAbort):
            # abort conditions are checked at the ticks of the clock, the
            # asynchronous forms too (as for single-clock assertions)
            cond = A.Binary("&&", t, self.E(n.cond, c))
            return A.PAbort(n.op, cond, self.P(n.prop, c, at, strength), span=n.span)
        if isinstance(n, A.PUnaryTemporal):
            st = "strong" if n.op.startswith("s_") else strength
            return A.PUnaryTemporal(n.op, self.P(n.prop, c, c, st), n.lo, n.hi, n.hi_inf, tick=t,
                                    span=n.span)
        self.err("unsupported property in a multi-clock assertion", n, SvaUnsupportedError)


def _case_to_if(node: A.PCase, rw: Rewriter):
    default = None
    chain = []
    for vals, p in node.items:
        if vals is None:
            default = p
        else:
            cond = None
            for v in vals:
                eq = A.Binary("==", node.expr, v, span=v.span)
                cond = eq if cond is None else A.Binary("||", cond, eq, span=v.span)
            chain.append((cond, p))
    result = default
    for cond, p in reversed(chain):
        result = A.PIf(cond, p, result, span=node.span)
    if result is None:
        rw.err("empty case property", node)
    return result


def strip_clocks(node):
    """Copy of a property without its clocking events (all the same clock)."""
    if isinstance(node, A.Clocked):
        return strip_clocks(node.body)
    if not isinstance(node, A.Node):
        return node
    new = copy.copy(node)
    for k, v in vars(node).items():
        if isinstance(v, A.Node):
            setattr(new, k, strip_clocks(v))
        elif isinstance(v, list):
            if isinstance(node, A.Call):
                v = [None if isinstance(x, A.ClockEvent) else x for x in v]
                while v and v[-1] is None:
                    v = v[:-1]
            setattr(new, k, [strip_clocks(x) if isinstance(x, A.Node)
                             else tuple(strip_clocks(y) if isinstance(y, A.Node)
                                        else [strip_clocks(z) for z in y] if isinstance(y, list) else y
                                        for y in x) if isinstance(x, tuple)
                             else x for x in v])
    return new
