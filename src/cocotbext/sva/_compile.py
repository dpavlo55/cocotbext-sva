"""Compile a parsed assertion into a Program: templates + atom evaluators.

Names are resolved through a Binder (supplied by the cocotb layer or by the
trace runner used in tests):

  binder.lookup(path)              -> SignalRef | ConstRef | None
  binder.x_slot(ref)               -> slot of the signal's X-mask reader
  binder.history_channel(key, value_code, gate_code, depth) -> channel index

Generated evaluator functions all take (c, R, cx, RX, H[, E]):
  c / R     per-tick value cache and readers, indexed by slot
  cx / RX   the same for X masks
  H         history-channel values for this tick
  E         local-variable env (env atoms and actions only)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import _ast as A
from ._errors import SvaError, SvaNameError, SvaUnsupportedError
from ._expr import (HELPERS, Code, ConstRef, ExprCompiler, LocalRef, Resolver, SignalRef,
                    TriggeredRef, mask, walk)
from ._nfa import Builder
from ._prop import (FAIL, VPASS, AbortT, AlwaysT, AndT, ConstT, EventuallyT, FollowedByT, IffT, IfT,
                    ImpliesT, ImplT, NextT, NotT, OrT, SeqT, UntilT)


@dataclass
class NamedDef:
    """A named sequence or property (optionally with formal arguments)."""
    name: str
    ast: A.Node
    args: tuple = ()
    text: str = ""


@dataclass
class Program:
    text: str
    kind: str                          # 'assert' | 'assume' | 'cover'
    tmpl: object
    envmode: bool
    atoms: list                        # base atoms: Code (boolean)
    env_atoms: list                    # Code (boolean, may use E)
    actions: list                      # (Code value, width)
    nlocals: int
    local_names: list
    disable_bit: int                   # 0 if no disable iff
    start_bit: int
    clock: object                      # A.ClockEvent | None
    slots: set = field(default_factory=set)   # all value slots read by atoms
    warnings: list = field(default_factory=list)
    contradiction: bool = False           # some transition was dropped as unsatisfiable
    _bitsfn_cache: dict = field(default_factory=dict, repr=False)
    _slots_cache: dict = field(default_factory=dict, repr=False)
    _envfns: list = field(default_factory=list, repr=False)
    _actfns: list = field(default_factory=list, repr=False)

    def bits_fn(self, m: int):
        """Function computing the base-atom bits in mask m (plus the disable bit)."""
        fn = self._bitsfn_cache.get(m)
        if fn is None:
            idx = [i for i in range(len(self.atoms)) if m >> i & 1]
            fn = _gen_bits_fn([self.atoms[i] for i in idx], idx)
            self._bitsfn_cache[m] = fn
        return fn

    def slots_of(self, m: int):
        """(value slots, X slots) read by the base atoms in mask m."""
        hit = self._slots_cache.get(m)
        if hit is None:
            sl, xs = set(), set()
            for i, code in enumerate(self.atoms):
                if m >> i & 1:
                    sl |= code.slots
                    xs |= code.xslots
            hit = self._slots_cache[m] = (frozenset(sl), frozenset(xs))
        return hit

    def all_slots(self):
        hit = self._slots_cache.get("all")
        if hit is None:
            sl, xs = set(), set()
            for code in self.atoms + self.env_atoms + [c for c, _ in self.actions]:
                sl |= code.slots
                xs |= code.xslots
            hit = self._slots_cache["all"] = (frozenset(sl), frozenset(xs))
        return hit

    def env_fns(self):
        if not self._envfns and self.env_atoms:
            self._envfns = [_gen_value_fn(c, with_env=True) for c in self.env_atoms]
        if not self._actfns and self.actions:
            self._actfns = [_gen_value_fn(c, with_env=True, width=w) for c, w in self.actions]
        return self._envfns, self._actfns


def _prelude(code: Code) -> list[str]:
    lines = []
    for s in sorted(code.slots):
        lines.append(f"    v{s} = c[{s}]")
        lines.append(f"    if v{s} is None: v{s} = c[{s}] = R[{s}]()")
    for s in sorted(code.xslots):
        lines.append(f"    x{s} = cx[{s}]")
        lines.append(f"    if x{s} is None: x{s} = cx[{s}] = RX[{s}]()")
    return lines


def _compile_fn(src: str, name: str):
    ns = dict(HELPERS)
    exec(compile(src, f"<sva:{name}>", "exec"), ns)
    return ns[name]


def _gen_bits_fn(codes: list, indices: list):
    total = Code("")
    for c in codes:
        total.merge(c)
    lines = ["def _bits(c, R, cx, RX, H):"] + _prelude(total)
    if not codes:
        lines.append("    return 0")
    else:
        terms = [f"(({c.text}) << {i})" if i else f"({c.text})" for c, i in zip(codes, indices)]
        lines.append("    return " + " | ".join(terms))
    return _compile_fn("\n".join(lines), "_bits")


def _gen_value_fn(code: Code, with_env: bool = False, width: int | None = None):
    sig = "c, R, cx, RX, H, E" if with_env else "c, R, cx, RX, H"
    lines = [f"def _val({sig}):"] + _prelude(code)
    expr = code.text if width is None else f"(({code.text}) & {mask(width)})"
    lines.append(f"    return {expr}")
    return _compile_fn("\n".join(lines), "_val")


def gen_value_fn(code: Code, width: int | None = None):
    return _gen_value_fn(code, with_env=False, width=width)


# --------------------------------------------------------------------------
# The compiler
# --------------------------------------------------------------------------

class _Ctx(Resolver):
    def __init__(self, binder, text: str, named: dict, local_names: set):
        self.binder = binder
        self.text = text
        self.named = named
        self.local_names = local_names
        self.locals: dict[str, int] = {}
        self.local_exprs: dict[str, A.Node] = {}
        self.ec = ExprCompiler(self, text)
        self.atoms: list[Code] = []
        self.atom_index: dict[str, int] = {}
        self.env_atoms: list[Code] = []
        self.env_index: dict[str, int] = {}
        self.actions: list = []
        self.b = Builder()

    # Resolver interface
    def resolve(self, ident: A.Id):
        name = ident.path[0]
        if len(ident.path) == 1 and name in self.local_names:
            if name not in self.locals:
                self.locals[name] = len(self.locals)
            return LocalRef(self.locals[name], name)
        if len(ident.path) == 2 and ident.path[1] in ("triggered", "ended") and name in self.named:
            return TriggeredRef(name)
        ref = self.binder.lookup(ident.path)
        if ref is None:
            raise SvaNameError(self._name_error(ident))
        return ref

    def _name_error(self, ident: A.Id) -> str:
        msg = f"unknown name '{ident.name}'"
        hint = getattr(self.binder, "suggest", None)
        if hint:
            s = hint(ident.path)
            if s:
                msg += f" (did you mean {', '.join(repr(x) for x in s)}?)"
        if self.text:
            st, en = ident.span
            msg += f"\n    {self.text}\n    {' ' * st}{'^' * max(1, en - st)}"
        return msg

    def local_width(self, ref: LocalRef):
        expr = self.local_exprs.get(ref.name)
        if expr is None:
            raise SvaError(f"local variable '{ref.name}' is used but never assigned")
        return self.ec.type(expr)

    def x_slot(self, ref: SignalRef) -> int:
        return self.binder.x_slot(ref)

    def history_channel(self, kind, expr, depth, gate):
        code, w, s = self.ec.value(expr)
        gcode = self.ec.boolean(gate) if gate is not None else None
        key = (code.text, w, depth, gcode.text if gcode else None)
        return self.binder.history_channel(key, code, w, gcode, depth)

    def triggered_channel(self, name):
        raise SvaUnsupportedError("'.triggered' is not supported yet")

    # atoms
    def atom(self, code: Code) -> tuple[bool, int]:
        """Register a boolean Code; returns (is_env, index)."""
        if code.uses_env:
            i = self.env_index.get(code.text)
            if i is None:
                i = self.env_index[code.text] = len(self.env_atoms)
                self.env_atoms.append(code)
            return True, i
        i = self.atom_index.get(code.text)
        if i is None:
            i = self.atom_index[code.text] = len(self.atoms)
            self.atoms.append(code)
        return False, i

    def guard(self, expr: A.Node):
        """Guard (pos, neg, envlits) for a boolean expression, or True/False if constant."""
        lits = []
        self._literals(expr, True, lits)
        pos = neg = 0
        envlits = []
        for e, pol in lits:
            const = self._const_bool(e)
            if const is not None:
                if const != pol:
                    return False
                continue
            is_env, i = self.atom(self.ec.boolean(e))
            if is_env:
                envlits.append((i, pol))
            elif pol:
                pos |= 1 << i
            else:
                neg |= 1 << i
        if not pos and not neg and not envlits:
            return True
        return (pos, neg, tuple(envlits))

    def false_guard(self):
        """A guard that is never true but still a transition (see merge_ops)."""
        _, i = self.atom(Code("False"))
        return (1 << i, 0, ())

    def _literals(self, e, pol, out):
        if isinstance(e, A.Unary) and e.op == "!":
            self._literals(e.operand, not pol, out)
        elif pol and isinstance(e, A.Binary) and e.op == "&&":
            self._literals(e.left, True, out)
            self._literals(e.right, True, out)
        else:
            out.append((e, pol))

    def _const_bool(self, e):
        try:
            return self.ec.const(e) != 0
        except SvaError:
            return None


class Compiler:
    def __init__(self, binder, text: str, named: dict | None = None):
        self.text = text
        self.named = dict(named or {})
        self.binder = binder
        self.warnings: list[str] = []

    def compile(self, spec: A.AssertionSpec, kind: str = "assert",
                default_disable: A.Node | None = None) -> Program:
        local_names = _assigned_locals(spec.prop, self.named)
        ctx = _Ctx(self.binder, self.text, self.named, local_names)
        self.ctx = ctx
        disable = spec.disable if spec.disable is not None else default_disable
        disable_atom = None
        if disable is not None:
            code = ctx.ec.boolean(disable)
            if code.uses_env:
                raise SvaError("disable iff cannot use local variables")
            disable_atom = ctx.atom(code)[1]
        self.envmode = bool(local_names)
        strength = "strong" if kind == "cover" else "weak"
        from ._nfa import CONTRADICTION
        CONTRADICTION["seen"] = False
        tmpl = self.prop(spec.prop, strength)
        contradiction = CONTRADICTION["seen"]
        natoms = len(ctx.atoms)
        prog = Program(
            text=self.text, kind=kind, tmpl=tmpl, envmode=self.envmode,
            atoms=ctx.atoms, env_atoms=ctx.env_atoms, actions=ctx.actions,
            nlocals=len(ctx.locals), local_names=sorted(ctx.locals, key=ctx.locals.get),
            disable_bit=(1 << disable_atom) if disable_atom is not None else 0,
            start_bit=1 << natoms, clock=spec.clock,
        )
        prog.warnings = list(self.warnings)
        prog.contradiction = contradiction
        for c in ctx.atoms + ctx.env_atoms:
            prog.slots |= c.slots
        for c, _ in ctx.actions:
            prog.slots |= c.slots
        return prog

    # -- properties ---------------------------------------------------------
    def prop(self, node: A.Node, strength: str):
        env = self.envmode
        if isinstance(node, A.PSeq):
            nfa = self.nfa(node.seq)
            if nfa.nullable:
                raise SvaError(self._at("a sequence used as a property must not admit an empty match", node))
            st = node.strength or strength
            return SeqT(nfa, st == "strong", env)
        if A.is_seq(node) or A.is_expr(node):
            return self.prop(A.PSeq(node if A.is_seq(node) else A.SBool(node, span=node.span),
                                    span=node.span), strength)
        if isinstance(node, A.PImpl):
            ante = self.nfa(node.seq)
            if node.op in ("|->", "|=>"):
                return ImplT(ante, self.prop(node.prop, strength), node.op == "|->", env)
            return FollowedByT(ante, self.prop(node.prop, strength), node.op == "#-#", env)
        if isinstance(node, A.PNot):
            # The default strength of a sequence comes from the directive only
            # (IEEE 1800 16.12.2); `not` does not change it.
            return NotT(self.prop(node.prop, strength))
        if isinstance(node, A.PBin):
            if node.op == "and":
                return AndT(self.prop(node.left, strength), self.prop(node.right, strength))
            if node.op == "or":
                return OrT(self.prop(node.left, strength), self.prop(node.right, strength))
            if node.op == "implies":
                return ImpliesT(self.prop(node.left, strength), self.prop(node.right, strength))
            if node.op == "iff":
                p, q = node.left, node.right
                return IffT(self.prop(p, strength), self.prop(q, strength))
            if node.op in ("until", "s_until", "until_with", "s_until_with"):
                strong = node.op.startswith("s_")
                st = "strong" if strong else strength
                return UntilT(self.prop(node.left, st), self.prop(node.right, st),
                              node.op.endswith("_with"), strong, env)
            raise SvaUnsupportedError(self._at(f"'{node.op}' is not supported yet", node))
        if isinstance(node, A.PIf):
            g = self.ctx.guard(node.cond)
            p = self.prop(node.then, strength)
            q = self.prop(node.other, strength) if node.other is not None else None
            if g is True:
                return p
            if g is False:
                return q if q is not None else ConstT(VPASS, env)
            return IfT(g, p, q, env)
        if isinstance(node, A.PCase):
            return self._case(node, strength)
        if isinstance(node, A.PUnaryTemporal):
            return self._temporal(node, strength)
        if isinstance(node, A.PAbort):
            g = self.ctx.guard(node.cond)
            if g is not True and g is not False and g[2]:
                raise SvaError(self._at(f"the condition of {node.op} cannot use local variables", node.cond))
            p = self.prop(node.prop, strength)
            accept = node.op.endswith("accept_on")
            if g is False:
                return p
            if g is True:
                g = (0, 0, ())          # always true (a vacuous success still stays vacuous)
            return AbortT(g, p, accept)
        raise SvaUnsupportedError(self._at("unsupported property", node))

    def _temporal(self, node: A.PUnaryTemporal, strength):
        op = node.op
        strong = op.startswith("s_")
        base = op[2:] if strong else op
        # a strong operator makes the default strength of its operand strong
        p = self.prop(node.prop, "strong" if strong else strength)
        env = self.envmode
        if base == "nexttime":
            n = 1 if node.lo is None else self._count(node.lo)
            return p if n == 0 else NextT(p, n, strong, env)
        if node.lo is None:
            if base == "eventually" and not strong:
                raise SvaError(self._at("'eventually' needs a range: eventually [m:n] (or use s_eventually)", node))
            if base == "always" and strong:
                raise SvaError(self._at("'s_always' needs a bounded range: s_always [m:n]", node))
            lo, hi = 0, None
        else:
            lo = self._count(node.lo)
            hi = None if node.hi_inf else self._count(node.hi)
            if hi is not None and hi < lo:
                raise SvaError(self._at("range must have lo <= hi", node))
            if hi is None and (base == "always") == strong:
                raise SvaError(self._at(f"'{op}' needs a bounded range", node))
        if base == "always":
            return AlwaysT(p, lo, hi, strong, env)
        return EventuallyT(p, lo, hi, strong, env)

    def _case(self, node: A.PCase, strength):
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
            raise SvaError(self._at("empty case property", node))
        return self.prop(result, strength)

    # -- sequences ----------------------------------------------------------
    def nfa(self, seq: A.Node):
        b = self.ctx.b
        frag = self.frag(seq)
        nfa = b.finish(frag)
        if not nfa.final:
            msg = ("this sequence can only match empty (an implication with it is always vacuous)"
                   if nfa.nullable else "this sequence can never match")
            self.warnings.append(self._at(msg, seq))
        return nfa

    def frag(self, node: A.Node):
        b = self.ctx.b
        if A.is_expr(node):
            node = A.SBool(node, span=node.span)
        if isinstance(node, A.SBool):
            expanded = self._named_seq(node.expr)
            if expanded is not None:
                return self.frag(expanded)
            g = self.ctx.guard(node.expr)
            if g is True:
                return b.true()
            if g is False:
                from ._nfa import CONTRADICTION
                CONTRADICTION["seen"] = True
                return b.never()
            return b.guard(*g)
        if isinstance(node, A.SDelay):
            lo = self._count(node.lo)
            hi = None if node.hi_inf else self._count(node.hi)
            if hi is not None and hi < lo:
                raise SvaError(self._at("delay range must have lo <= hi", node))
            make_left = (lambda: self.frag(node.left)) if node.left is not None else b.true
            return self._delay(make_left, lo, hi, node.right)
        if isinstance(node, A.SRepeat):
            lo = self._count(node.lo)
            hi = None if node.hi_inf else self._count(node.hi)
            if hi is not None and hi < lo:
                raise SvaError(self._at("repetition range must have lo <= hi", node))
            if node.kind == "*":
                return self._repeat(lambda: self.frag(node.seq), lo, hi)
            # goto / non-consecutive repetition of a boolean
            expr = node.seq.expr if isinstance(node.seq, A.SBool) else node.seq
            notb = A.Unary("!", expr, span=expr.span)

            def one():
                return b.concat(b.star(self.frag(A.SBool(notb, span=expr.span))),
                                self.frag(A.SBool(expr, span=expr.span)))

            f = self._repeat(one, lo, hi)
            if node.kind == "=":
                f = b.concat(f, b.star(self.frag(A.SBool(notb, span=expr.span))))
            return f
        if isinstance(node, A.SBin):
            if node.op == "or":
                return b.union(self.frag(node.left), self.frag(node.right))
            if node.op == "and":
                return b.and_(self.frag(node.left), self.frag(node.right))
            if node.op == "intersect":
                return b.intersect(self.frag(node.left), self.frag(node.right))
            if node.op == "within":
                # (1[*0:$] ##1 S1 ##1 1[*0:$]) intersect S2
                outer = b.concat(b.concat(b.star(b.true()), self.frag(node.left)), b.star(b.true()))
                return b.intersect(outer, self.frag(node.right))
        if isinstance(node, A.SThroughout):
            return b.intersect(b.star(self.frag(A.SBool(node.expr, span=node.expr.span))),
                               self.frag(node.seq))
        if isinstance(node, A.SFirstMatch):
            return b.first_match(self.frag(node.seq))
        if isinstance(node, A.SMatchItems):
            f = self.frag(node.seq)
            assigns = []
            for name, expr in node.assigns:
                self.ctx.local_exprs.setdefault(name, expr)
                li = self.ctx.resolve(A.Id((name,)))
                code, w, _ = self.ctx.ec.value(expr)
                lw, _ = self.ctx.local_width(li)
                self.ctx.actions.append((code, lw))
                assigns.append((li.index, len(self.ctx.actions) - 1))
            return b.with_actions(f, tuple(assigns))
        if isinstance(node, A.PSeq) and node.strength:
            raise SvaError(self._at(f"{node.strength}() is a property, not a sequence", node))
        raise SvaError(self._at("expected a sequence", node))

    def _delay(self, make_left, lo, hi, right_node):
        """S1 ##[lo:hi] S2 (hi None = $); S1 is rebuilt per use via make_left."""
        b = self.ctx.b
        if lo == 0:
            fused = b.fuse(make_left(), self.frag(right_node))
            if hi == 0:
                return fused
            return b.union(fused, self._delay(make_left, 1, hi, right_node))
        mid = make_left()
        for _ in range(lo - 1):
            mid = b.concat(mid, b.true())
        if hi is None:
            mid = b.concat(mid, b.star(b.true()))
        elif hi > lo:
            for _ in range(hi - lo):
                mid = b.concat(mid, b.optional(b.true()))
        return b.concat(mid, self.frag(right_node))

    def _repeat(self, make, lo, hi):
        b = self.ctx.b
        f = b.empty()
        for _ in range(lo):
            f = b.concat(f, make())
        if hi is None:
            f = b.concat(f, b.star(make()))
        else:
            for _ in range(hi - lo):
                f = b.concat(f, b.optional(make()))
        return f

    def _count(self, node) -> int:
        v = self.ctx.ec.const(node)
        if v < 0:
            raise SvaError(self._at("delay and repetition counts must be >= 0", node))
        return v

    def _named_seq(self, expr):
        """Expand a reference to a named sequence (Id or Call)."""
        if isinstance(expr, A.Id) and len(expr.path) == 1 and expr.path[0] in self.named:
            d = self.named[expr.path[0]]
            if d.args:
                raise SvaError(self._at(f"'{d.name}' needs {len(d.args)} argument(s)", expr))
            return d.ast
        if isinstance(expr, A.Call) and expr.name in self.named:
            d = self.named[expr.name]
            if len(expr.args) != len(d.args):
                raise SvaError(self._at(f"'{d.name}' takes {len(d.args)} argument(s)", expr))
            return substitute(d.ast, dict(zip(d.args, expr.args)))
        return None

    def _at(self, msg, node):
        if self.text and node is not None:
            s, e = node.span
            return f"{msg}\n    {self.text}\n    {' ' * s}{'^' * max(1, e - s)}"
        return msg


def _flip(strength: str) -> str:
    return "strong" if strength == "weak" else "weak"


def _assigned_locals(node, named) -> set:
    names = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, A.SMatchItems):
            names |= {a for a, _ in n.assigns}
        if isinstance(n, A.Node):
            for v in vars(n).values():
                if isinstance(v, A.Node):
                    stack.append(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, A.Node):
                            stack.append(item)
                        elif isinstance(item, tuple):
                            stack += [x for x in item if isinstance(x, A.Node)]
    return names


def substitute(node, mapping: dict):
    """Copy of node with formal-argument identifiers replaced."""
    import copy
    if isinstance(node, A.Id) and len(node.path) == 1 and node.path[0] in mapping:
        return mapping[node.path[0]]
    if isinstance(node, A.SBool) and isinstance(node.expr, A.Id) and len(node.expr.path) == 1 \
            and node.expr.path[0] in mapping:
        return mapping[node.expr.path[0]]
    if not isinstance(node, A.Node):
        return node
    new = copy.copy(node)
    for k, v in vars(node).items():
        if isinstance(v, A.Node):
            setattr(new, k, substitute(v, mapping))
        elif isinstance(v, list):
            setattr(new, k, [substitute(x, mapping) if isinstance(x, A.Node)
                             else tuple(substitute(y, mapping) for y in x) if isinstance(x, tuple)
                             else x for x in v])
    return new
