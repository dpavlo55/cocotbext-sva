"""Expression typing (IEEE 1800 §11.6 width and sign rules) and code generation.

Values are Python ints holding the unsigned bit pattern of the value at its
width. Unknown (X/Z) bits read as 0 (documented limitation; `$isunknown` and
`$countbits` see X through a separate X-mask read).

Code generation produces a Python expression string over these names:
  v<slot>   sampled value of a signal / Python value (bound in a prelude)
  x<slot>   X mask of a signal (only for $isunknown / $countbits)
  H[k]      history channel k ($past, $rose, ..., .triggered)
  E[k]      local variable k of the current thread
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import _ast as A
from ._errors import SvaError, SvaNameError, SvaUnsupportedError


# --------------------------------------------------------------------------
# Resolved references (produced by the resolver supplied by the compiler)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalRef:
    slot: int
    width: int
    signed: bool = False
    left: int | None = None        # declared index range (left, right); None -> [width-1:0]
    right: int | None = None
    enum: dict | None = None       # literal name (lower-case) -> int, for enum-typed signals
    label: str = ""


@dataclass(frozen=True)
class ConstRef:
    value: int
    width: int | None = None       # None: unsized integer (32-bit signed)
    signed: bool = True


@dataclass(frozen=True)
class LocalRef:
    index: int
    name: str


@dataclass(frozen=True)
class TriggeredRef:
    seq_name: str


class Resolver:
    """Interface; the compiler provides the implementation."""

    def resolve(self, ident: A.Id):            # -> SignalRef | ConstRef | LocalRef | TriggeredRef
        raise NotImplementedError

    def local_width(self, ref: LocalRef) -> tuple[int, bool]:
        raise NotImplementedError

    def history_channel(self, kind: str, expr: A.Node, depth: int, gate) -> int:
        raise NotImplementedError

    def triggered_channel(self, name: str) -> int:
        raise NotImplementedError

    def x_slot(self, ref: SignalRef) -> int:
        raise NotImplementedError


@dataclass
class Code:
    """Generated Python expression plus the inputs it reads."""
    text: str
    slots: set = field(default_factory=set)
    xslots: set = field(default_factory=set)
    chans: set = field(default_factory=set)
    uses_env: bool = False

    def merge(self, *others: "Code") -> "Code":
        for o in others:
            self.slots |= o.slots
            self.xslots |= o.xslots
            self.chans |= o.chans
            self.uses_env |= o.uses_env
        return self


def mask(w: int) -> int:
    return (1 << w) - 1


# Helpers available to generated code.
def _s(x: int, w: int) -> int:
    """Unsigned bit pattern -> signed int."""
    return x - (1 << w) if x >> (w - 1) & 1 else x


def _sx(x: int, wf: int, wt: int) -> int:
    """Sign-extend a wf-bit pattern to wt bits."""
    if x >> (wf - 1) & 1:
        return x | (mask(wt) ^ mask(wf))
    return x


def _sdiv(a: int, b: int, w: int) -> int:
    if b == 0:
        return 0
    a, b = _s(a, w), _s(b, w)
    q = abs(a) // abs(b)
    return (-q if (a < 0) != (b < 0) else q) & mask(w)


def _smod(a: int, b: int, w: int) -> int:
    if b == 0:
        return 0
    a, b = _s(a, w), _s(b, w)
    r = abs(a) % abs(b)
    return (-r if a < 0 else r) & mask(w)


def _pos(i: int, n: int, left: int, right: int) -> int:
    """Bit position (0 = LSB) of declared index i in a range left..right of n bits."""
    p = (n - 1) - (i - left if right >= left else left - i)
    if p < 0 or p >= n:
        return n  # out of range -> reads 0 (X in SV)
    return p


HELPERS = {"_s": _s, "_sx": _sx, "_sdiv": _sdiv, "_smod": _smod, "_pos": _pos}

_SYS_1BIT = {"$rose", "$fell", "$stable", "$changed", "$onehot", "$onehot0", "$isunknown"}


class ExprCompiler:
    def __init__(self, resolver: Resolver, text: str = ""):
        self.r = resolver
        self.text = text

    # -- errors -------------------------------------------------------------
    def err(self, msg: str, node: A.Node, cls=SvaError):
        if self.text:
            s, e = node.span
            msg = f"{msg}\n    {self.text}\n    {' ' * s}{'^' * max(1, e - s)}"
        raise cls(msg)

    # -- constants ----------------------------------------------------------
    def const(self, node: A.Node) -> int:
        """Evaluate a constant expression (ranges, indices, repetition counts)."""
        if isinstance(node, A.Num):
            if node.fill is not None:
                self.err("unbased literal is not allowed here", node)
            return node.value
        if isinstance(node, A.Id):
            ref = self.r.resolve(node)
            if isinstance(ref, ConstRef):
                return ref.value
            self.err(f"'{node.name}' is not a constant", node)
        if isinstance(node, A.Unary) and node.op in ("-", "+"):
            v = self.const(node.operand)
            return -v if node.op == "-" else v
        if isinstance(node, A.Binary):
            a, b = self.const(node.left), self.const(node.right)
            ops = {"+": a + b, "-": a - b, "*": a * b}
            if node.op in ops:
                return ops[node.op]
            if node.op == "/" and b:
                return a // b
            if node.op == "**":
                return a ** b
        if isinstance(node, A.Call) and node.name == "$clog2":
            v = self.const(node.args[0])
            return max(0, (v - 1).bit_length())
        if isinstance(node, A.Call) and node.name == "$bits":
            return self.type(node.args[0])[0]
        self.err("expected a constant expression", node)

    # -- self-determined type -----------------------------------------------
    def type(self, node: A.Node) -> tuple[int, bool]:
        """(width, signed) of node in a self-determined context."""
        if isinstance(node, A.Num):
            if node.fill is not None:
                return 1, False
            return (node.width or 32), node.signed
        if isinstance(node, A.Id):
            ref = self.r.resolve(node)
            if isinstance(ref, SignalRef):
                return ref.width, ref.signed
            if isinstance(ref, ConstRef):
                return (ref.width or 32), ref.signed
            if isinstance(ref, LocalRef):
                return self.r.local_width(ref)
            if isinstance(ref, TriggeredRef):
                return 1, False
            self.err(f"cannot use '{node.name}' here", node)
        if isinstance(node, A.Unary):
            if node.op in ("+", "-", "~"):
                return self.type(node.operand)
            return 1, False
        if isinstance(node, A.Binary):
            op = node.op
            if op in ("+", "-", "*", "/", "%", "&", "|", "^", "~^", "^~"):
                wa, sa = self.type(node.left)
                wb, sb = self.type(node.right)
                return max(wa, wb), sa and sb
            if op in ("<<", ">>", "<<<", ">>>", "**"):
                return self.type(node.left)
            return 1, False
        if isinstance(node, A.Cond):
            wa, sa = self.type(node.then)
            wb, sb = self.type(node.other)
            return max(wa, wb), sa and sb
        if isinstance(node, A.Concat):
            return sum(self.type(i)[0] for i in node.items), False
        if isinstance(node, A.Repl):
            return self.const(node.count) * sum(self.type(i)[0] for i in node.items), False
        if isinstance(node, A.Index):
            return 1, False
        if isinstance(node, A.Slice):
            return abs(self.const(node.msb) - self.const(node.lsb)) + 1, False
        if isinstance(node, A.PartSelect):
            return self.const(node.width), False
        if isinstance(node, A.Call):
            n = node.name
            if n in _SYS_1BIT:
                return 1, False
            if n in ("$past", "$sampled"):
                return self.type(node.args[0])
            if n in ("$countones", "$countbits", "$bits", "$clog2"):
                return 32, True
            if n == "$signed":
                return self.type(node.args[0])[0], True
            if n == "$unsigned":
                return self.type(node.args[0])[0], False
            self.err(f"unsupported function {n}", node, SvaUnsupportedError)
        self.err("unsupported expression", node, SvaUnsupportedError)

    # -- code generation ----------------------------------------------------
    def boolean(self, node: A.Node) -> Code:
        """Code for `node` used as a condition (true if nonzero)."""
        w, s = self.type(node)
        c = self.gen(node, w, s)
        if _is_boolish(node):
            return c
        c.text = f"({c.text} != 0)"
        return c

    def value(self, node: A.Node) -> tuple[Code, int, bool]:
        """Code for `node` as a value in its self-determined type."""
        w, s = self.type(node)
        return self.gen(node, w, s), w, s

    def gen(self, node: A.Node, w: int, s: bool) -> Code:
        """Code yielding node's value as a w-bit unsigned pattern (context type w, s)."""
        g = getattr(self, "_gen_" + type(node).__name__, None)
        if g is None:
            self.err("unsupported expression", node, SvaUnsupportedError)
        return g(node, w, s)

    def _extend(self, code: Code, wf: int, sf: bool, w: int, s: bool) -> Code:
        if w > wf and s and sf:
            code.text = f"_sx({code.text}, {wf}, {w})"
        elif w < wf:
            code.text = f"({code.text} & {mask(w)})"
        return code

    def _gen_Num(self, node: A.Num, w, s):
        if node.fill is not None:
            return Code(str(mask(w) if node.fill == "1" else 0))
        wf = node.width or 32
        v = node.value & mask(wf)
        if s and node.signed and v >> (wf - 1) & 1:
            v = _sx(v, wf, w)
        return Code(str(v & mask(w)))

    def _gen_Id(self, node: A.Id, w, s):
        ref = self.r.resolve(node)
        if isinstance(ref, SignalRef):
            return self._extend(Code(f"v{ref.slot}", slots={ref.slot}), ref.width, ref.signed, w, s)
        if isinstance(ref, ConstRef):
            wf = ref.width or 32
            v = ref.value & mask(wf)
            if s and ref.signed and v >> (wf - 1) & 1:
                v = _sx(v, wf, w)
            return Code(str(v & mask(w)))
        if isinstance(ref, LocalRef):
            wf, sf = self.r.local_width(ref)
            return self._extend(Code(f"E[{ref.index}]", uses_env=True), wf, sf, w, s)
        if isinstance(ref, TriggeredRef):
            k = self.r.triggered_channel(ref.seq_name)
            return Code(f"H[{k}]", chans={k})
        self.err(f"cannot use '{node.name}' here", node)

    def _gen_Unary(self, node: A.Unary, w, s):
        op = node.op
        if op in ("+", "-", "~"):
            c = self.gen(node.operand, w, s)
            if op == "+":
                return c
            if op == "-":
                c.text = f"((-{c.text}) & {mask(w)})"
            else:
                c.text = f"((~{c.text}) & {mask(w)})"
            return c
        wo, so = self.type(node.operand)
        c = self.gen(node.operand, wo, so)
        t = c.text
        m = mask(wo)
        c.text = {
            "!": f"({t} == 0)",
            "&": f"({t} == {m})",
            "~&": f"({t} != {m})",
            "|": f"({t} != 0)",
            "~|": f"({t} == 0)",
            "^": f"(({t}).bit_count() & 1)",
            "~^": f"(1 - (({t}).bit_count() & 1))",
            "^~": f"(1 - (({t}).bit_count() & 1))",
        }[op]
        return c

    def _gen_Binary(self, node: A.Binary, w, s):
        op = node.op
        m = mask(w)
        if op in ("+", "-", "*", "&", "|", "^", "~^", "^~", "/", "%"):
            a = self.gen(node.left, w, s)
            b = self.gen(node.right, w, s)
            ta, tb = a.text, b.text
            if op in ("+", "-", "*"):
                text = f"(({ta} {op} {tb}) & {m})"
            elif op in ("&", "|", "^"):
                text = f"({ta} {op} {tb})"
            elif op in ("~^", "^~"):
                text = f"((~({ta} ^ {tb})) & {m})"
            elif op == "/":
                text = f"_sdiv({ta}, {tb}, {w})" if s else f"(lambda _a, _b: _a // _b if _b else 0)({ta}, {tb})"
            else:
                text = f"_smod({ta}, {tb}, {w})" if s else f"(lambda _a, _b: _a % _b if _b else 0)({ta}, {tb})"
            return Code(text).merge(a, b)
        if op in ("<<", ">>", "<<<", ">>>", "**"):
            a = self.gen(node.left, w, s)
            wb, sb = self.type(node.right)
            b = self.gen(node.right, wb, False)
            if op in ("<<", "<<<"):
                text = f"(({a.text} << {b.text}) & {m})"
            elif op == ">>":
                text = f"({a.text} >> {b.text})"
            elif op == ">>>":
                text = f"((_s({a.text}, {w}) >> {b.text}) & {m})" if s else f"({a.text} >> {b.text})"
            else:
                text = f"(pow({a.text}, {b.text}) & {m})"
            return Code(text).merge(a, b)
        if op in ("<", "<=", ">", ">=", "==", "!=", "===", "!==", "==?", "!=?"):
            wa, sa = self.type(node.left)
            wb, sb = self.type(node.right)
            wo, so = max(wa, wb), sa and sb
            a = self.gen(node.left, wo, so)
            b = self._compare_rhs(node, wo, so)
            pyop = {"===": "==", "!==": "!=", "==?": "==", "!=?": "!="}.get(op, op)
            if op in ("==?", "!=?") and isinstance(node.right, A.Num) and node.right.xmask:
                keep = mask(wo) & ~node.right.xmask
                text = f"(({a.text} & {keep}) {pyop} ({b.text} & {keep}))"
            elif so and op in ("<", "<=", ">", ">="):
                text = f"(_s({a.text}, {wo}) {pyop} _s({b.text}, {wo}))"
            else:
                text = f"({a.text} {pyop} {b.text})"
            return Code(text).merge(a, b)
        if op in ("&&", "||"):
            a = self.boolean(node.left)
            b = self.boolean(node.right)
            pyop = "and" if op == "&&" else "or"
            return Code(f"({a.text} {pyop} {b.text})").merge(a, b)
        self.err(f"unsupported operator {op}", node, SvaUnsupportedError)

    def _compare_rhs(self, node: A.Binary, wo, so) -> Code:
        """Right operand of a comparison; resolves enum literals against the left side."""
        right = node.right
        if isinstance(right, A.Id) and len(right.path) == 1:
            lit = self._enum_literal(node.left, right.path[0])
            if lit is not None:
                return Code(str(lit))
        if isinstance(node.left, A.Id) and len(node.left.path) == 1:
            pass
        return self.gen(right, wo, so)

    def _enum_literal(self, other: A.Node, name: str):
        if not isinstance(other, A.Id):
            return None
        try:
            ref = self.r.resolve(other)
        except SvaNameError:
            return None
        if isinstance(ref, SignalRef) and ref.enum:
            return ref.enum.get(name.lower())
        return None

    def _gen_Cond(self, node: A.Cond, w, s):
        c = self.boolean(node.cond)
        a = self.gen(node.then, w, s)
        b = self.gen(node.other, w, s)
        return Code(f"({a.text} if {c.text} else {b.text})").merge(c, a, b)

    def _gen_Concat(self, node: A.Concat, w, s):
        parts = []
        shift = 0
        code = Code("")
        for item in reversed(node.items):
            wi, si = self.type(item)
            ci = self.gen(item, wi, False)
            code.merge(ci)
            parts.append(f"({ci.text} << {shift})" if shift else ci.text)
            shift += wi
        code.text = "(" + " | ".join(reversed(parts)) + ")"
        return self._extend(code, shift, False, w, s)

    def _gen_Repl(self, node: A.Repl, w, s):
        n = self.const(node.count)
        inner = self._gen_Concat(A.Concat(node.items, span=node.span), sum(self.type(i)[0] for i in node.items), False)
        wi = sum(self.type(i)[0] for i in node.items)
        parts = [f"({inner.text} << {k * wi})" if k else inner.text for k in range(n)]
        inner.text = "(" + " | ".join(parts) + ")"
        return self._extend(inner, n * wi, False, w, s)

    def _range_of(self, base: A.Node):
        """(n, left, right) declared range of a select base."""
        n, _ = self.type(base)
        if isinstance(base, A.Id):
            ref = self.r.resolve(base)
            if isinstance(ref, SignalRef) and ref.left is not None:
                return n, ref.left, ref.right
        return n, n - 1, 0

    def _static_pos(self, i: int, n: int, left: int, right: int, node) -> int:
        p = _pos(i, n, left, right)
        if p >= n:
            self.err(f"index {i} is outside the range [{left}:{right}]", node)
        return p

    def _gen_Index(self, node: A.Index, w, s):
        n, left, right = self._range_of(node.base)
        wb, sb = self.type(node.base)
        b = self.gen(node.base, wb, False)
        try:
            i = self.const(node.index)
        except SvaError:
            ic, _, _ = self.value(node.index)
            text = f"(({b.text} >> _pos({ic.text}, {n}, {left}, {right})) & 1)"
            return Code(text).merge(b, ic)
        p = self._static_pos(i, n, left, right, node)
        return Code(f"(({b.text} >> {p}) & 1)" if p else f"({b.text} & 1)").merge(b)

    def _gen_Slice(self, node: A.Slice, w, s):
        n, left, right = self._range_of(node.base)
        wb, _ = self.type(node.base)
        b = self.gen(node.base, wb, False)
        p1 = self._static_pos(self.const(node.msb), n, left, right, node)
        p2 = self._static_pos(self.const(node.lsb), n, left, right, node)
        lo, width = min(p1, p2), abs(p1 - p2) + 1
        text = f"(({b.text} >> {lo}) & {mask(width)})" if lo else f"({b.text} & {mask(width)})"
        return self._extend(Code(text).merge(b), width, False, w, s)

    def _gen_PartSelect(self, node: A.PartSelect, w, s):
        n, left, right = self._range_of(node.base)
        wb, _ = self.type(node.base)
        b = self.gen(node.base, wb, False)
        width = self.const(node.width)
        try:
            start = self.const(node.start)
        except SvaError:
            sc, _, _ = self.value(node.start)
            other = f"({sc.text} + {width - 1})" if node.up else f"({sc.text} - {width - 1})"
            lo = f"min(_pos({sc.text}, {n}, {left}, {right}), _pos({other}, {n}, {left}, {right}))"
            return self._extend(Code(f"(({b.text} >> {lo}) & {mask(width)})").merge(b, sc), width, False, w, s)
        end = start + width - 1 if node.up else start - width + 1
        p1 = self._static_pos(start, n, left, right, node)
        p2 = self._static_pos(end, n, left, right, node)
        lo = min(p1, p2)
        text = f"(({b.text} >> {lo}) & {mask(width)})" if lo else f"({b.text} & {mask(width)})"
        return self._extend(Code(text).merge(b), width, False, w, s)

    def _gen_Call(self, node: A.Call, w, s):
        n = node.name
        args = node.args
        if n == "$sampled":
            return self.gen(args[0], w, s)
        if n in ("$signed", "$unsigned"):
            wa, _ = self.type(args[0])
            c = self.gen(args[0], wa, False)
            return self._extend(c, wa, n == "$signed", w, s)
        if n == "$past":
            e = args[0]
            depth = 1
            if len(args) > 1 and args[1] is not None:
                depth = self.const(args[1])
                if depth < 1:
                    self.err("$past depth must be at least 1", node)
            gate = args[2] if len(args) > 2 else None
            if len(args) > 3 and args[3] is not None:
                self.err("$past with a clocking event is not supported here", node, SvaUnsupportedError)
            self._no_locals(e, node)
            k = self.r.history_channel("past", e, depth, gate)
            we, se = self.type(e)
            return self._extend(Code(f"(H[{k}] or 0)", chans={k}), we, se, w, s)
        if n in ("$rose", "$fell", "$stable", "$changed"):
            e = args[0]
            self._no_locals(e, node)
            # second argument: a clocking event in SVA; _multiclock replaces it
            # by a gating expression (the ticks of that clock)
            gate = args[1] if len(args) > 1 else None
            if isinstance(gate, A.ClockEvent):
                self.err(f"{n} with a clocking event is not supported here", node, SvaUnsupportedError)
            k = self.r.history_channel("past", e, 1, gate)
            we, se = self.type(e)
            cur = self.gen(e, we, se)
            p = f"H[{k}]"
            t = cur.text
            text = {
                "$rose": f"(({t} & 1) == 1 and ({p} is None or ({p} & 1) == 0))",
                "$fell": f"(({t} & 1) == 0 and ({p} is None or ({p} & 1) == 1))",
                "$stable": f"({p} is not None and {t} == {p})",
                "$changed": f"({p} is None or {t} != {p})",
            }[n]
            return Code(text, chans={k}).merge(cur)
        if n in ("$onehot", "$onehot0", "$countones"):
            we, se = self.type(args[0])
            c = self.gen(args[0], we, False)
            t = c.text
            if n == "$onehot":
                c.text = f"(lambda _v: _v != 0 and (_v & (_v - 1)) == 0)({t})"
            elif n == "$onehot0":
                c.text = f"(lambda _v: (_v & (_v - 1)) == 0)({t})"
            else:
                c.text = f"(({t}).bit_count())"
                return self._extend(c, 32, True, w, s)
            return c
        if n in ("$isunknown", "$countbits"):
            e = args[0]
            xm = self._xmask_code(e, node)
            if n == "$isunknown":
                return Code(f"({xm.text} != 0)").merge(xm)
            we, _ = self.type(e)
            val = self.gen(e, we, False)
            terms = []
            for ctl in args[1:]:
                if not isinstance(ctl, A.Num):
                    self.err("$countbits control arguments must be literals", node)
                if ctl.xmask or ctl.fill in ("x", "z"):
                    terms.append(f"({xm.text}).bit_count()")
                elif (ctl.fill == "1") or (ctl.fill is None and ctl.value & 1):
                    terms.append(f"({val.text} & ~{xm.text}).bit_count()")
                else:
                    terms.append(f"(~({val.text} | {xm.text}) & {mask(we)}).bit_count()")
            c = Code("(" + " + ".join(terms or ["0"]) + ")").merge(xm, val)
            return self._extend(c, 32, True, w, s)
        if n in ("$bits", "$clog2"):
            return Code(str(self.const(node) & mask(w)))
        self.err(f"unsupported function {n}", node, SvaUnsupportedError)

    def _xmask_code(self, e: A.Node, node) -> Code:
        """X mask of a signal or a concatenation of signals."""
        if isinstance(e, A.Id):
            ref = self.r.resolve(e)
            if isinstance(ref, SignalRef):
                xs = self.r.x_slot(ref)
                return Code(f"x{xs}", xslots={xs})
            return Code("0")
        if isinstance(e, A.Concat):
            parts, shift, code = [], 0, Code("")
            for item in reversed(e.items):
                ci = self._xmask_code(item, node)
                code.merge(ci)
                parts.append(f"({ci.text} << {shift})" if shift else ci.text)
                shift += self.type(item)[0]
            code.text = "(" + " | ".join(reversed(parts)) + ")"
            return code
        if isinstance(e, A.Num):
            return Code(str(e.xmask))
        self.err("X detection is supported on signals and concatenations of signals", node,
                 SvaUnsupportedError)

    def _no_locals(self, e: A.Node, node):
        for sub in walk(e):
            if isinstance(sub, A.Id):
                if isinstance(self.r.resolve(sub), LocalRef):
                    self.err("sampled-value functions cannot use local variables", node)


def _is_boolish(node: A.Node) -> bool:
    if isinstance(node, A.Binary) and node.op in ("<", "<=", ">", ">=", "==", "!=", "===", "!==",
                                                  "==?", "!=?", "&&", "||"):
        return True
    if isinstance(node, A.Unary) and node.op == "!":
        return True
    if isinstance(node, A.Call) and node.name in _SYS_1BIT:
        return True
    return False


def walk(node):
    """Yield node and all sub-expressions."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        yield n
        if isinstance(n, (A.Unary,)):
            stack.append(n.operand)
        elif isinstance(n, A.Binary):
            stack += [n.left, n.right]
        elif isinstance(n, A.Cond):
            stack += [n.cond, n.then, n.other]
        elif isinstance(n, (A.Concat,)):
            stack += n.items
        elif isinstance(n, A.Repl):
            stack += [n.count] + n.items
        elif isinstance(n, A.Index):
            stack += [n.base, n.index]
        elif isinstance(n, A.Slice):
            stack += [n.base, n.msb, n.lsb]
        elif isinstance(n, A.PartSelect):
            stack += [n.base, n.start, n.width]
        elif isinstance(n, A.Call):
            stack += [a for a in n.args if isinstance(a, A.Node) and not isinstance(a, A.ClockEvent)]
