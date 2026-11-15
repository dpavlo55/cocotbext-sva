"""Symbolic stepping of attempts with local variables.

An attempt of a property with local variables is a term that contains env
tuples (the local values of its threads). Concrete terms almost never repeat
(they carry captured data), so the fast path's transition caching does not
apply to them. Here an attempt is split into

  shape: the term with every env replaced by Slot(i), i = rank of the env in
         the sorted list of the term's distinct envs;
  envs:  that sorted list of distinct concrete envs.

The pair is canonical: equal concrete terms give equal (shape, envs), so
attempts still merge exactly as concrete terms do. Shapes are as finite as
env-free terms.

A transition is cached per (shape, base-atom inputs) as a decision tree over
the env-dependent conditions the step asks (env atoms on a slot or on an env
derived from one by this tick's assignments). A leaf holds the verdict, the
raw next shape (envs replaced by raw slots, in no particular order) and a
generated function computing the raw slots' concrete envs; a final
renumbering makes the result canonical. The tree is built lazily: a branch
missing at run time is filled by stepping the template symbolically again,
with the answers known along the path and the others evaluated concretely.
"""
from __future__ import annotations

from ._prop import _Pre, _Unstarted


class SEnv:
    """A symbolic env: a slot of the shape, or one derived from another by
    the assignments of one NFA transition."""
    __slots__ = ("key", "_h")

    def __eq__(self, other):
        return isinstance(other, SEnv) and other.key == self.key

    def __hash__(self):
        return self._h

    def __repr__(self):
        return f"SEnv{self.key}"


class Slot(SEnv):
    __slots__ = ("i",)

    def __init__(self, i):
        self.i = i
        self.key = ("s", i)
        self._h = hash(self.key)


class Derived(SEnv):
    __slots__ = ("parent", "assigns")

    def __init__(self, parent, assigns):
        self.parent = parent
        self.assigns = assigns
        self.key = ("a", parent.key, assigns)
        self._h = hash(self.key)


_SLOTS = [Slot(i) for i in range(64)]


def slot(i):
    while i >= len(_SLOTS):
        _SLOTS.append(Slot(len(_SLOTS)))
    return _SLOTS[i]


def tmap(t, f):
    """t with every SEnv/env leaf x replaced by f(x); unchanged parts keep
    their identity (templates compare some markers with `is`)."""
    tt = type(t)
    if tt is tuple:
        out = None
        for k, x in enumerate(t):
            y = tmap(x, f)
            if y is not x and out is None:
                out = list(t[:k])
            if out is not None:
                out.append(y)
        return t if out is None else tuple(out)
    if tt is frozenset:
        items = [tmap(x, f) for x in t]
        if all(a is b for a, b in zip(items, t)):
            return t
        return frozenset(items)
    if tt is _Unstarted or tt is _Pre:
        y = tmap(t.t, f)
        return t if y is t.t else tt(y)
    if isinstance(t, SEnv):
        return f(t)
    return t


def senvs_of(t, out: dict):
    """Collect the distinct SEnvs of a symbolic term, in order of appearance."""
    tt = type(t)
    if tt is tuple or tt is frozenset:
        for x in t:
            senvs_of(x, out)
    elif tt is _Unstarted or tt is _Pre:
        senvs_of(t.t, out)
    elif isinstance(t, SEnv):
        if t not in out:
            out[t] = len(out)
    return out


def ekey(e):
    """Sort key of a concrete env (values are ints or None)."""
    return tuple((0, 0) if v is None else (1, v) for v in e)


class SymVal:
    """Valuation for a symbolic step: base bits are concrete; env atoms are
    answered from `answers` or, if unknown, evaluated concretely by
    `resolve` and recorded in `path` (in the order first asked)."""
    __slots__ = ("bits", "answers", "path", "resolve")

    def __init__(self, bits, answers, resolve):
        self.bits = bits
        self.answers = answers
        self.path = []
        self.resolve = resolve

    def env_atom(self, idx, env):
        q = (idx, env)
        r = self.answers.get(q)
        if r is None:
            r = bool(self.resolve(idx, env))
            self.answers[q] = r
            self.path.append((q, r))
        return r

    def assign(self, e, assigns):
        return Derived(e, assigns)


class _Gen:
    """Generated code computing the concrete envs of SEnvs from E (the
    shape's concrete envs) and the tick's signal values."""

    def __init__(self, nlocals):
        self.nlocals = nlocals
        self.lines = []
        self.names = {}

    def env(self, s):
        name = self.names.get(s)
        if name is not None:
            return name
        if type(s) is Slot:
            name = f"e{len(self.names)}"
            self.lines.append(f"    {name} = E[{s.i}]")
        else:
            p = self.env(s.parent)
            vals = {local: f"A{act}(c, R, cx, RX, H, {p})" for local, act in s.assigns}
            name = f"e{len(self.names)}"
            items = [vals.get(k, f"{p}[{k}]") for k in range(self.nlocals)]
            self.lines.append(f"    {name} = ({', '.join(items)},)")
        self.names[s] = name
        return name


class SymCache:
    """Shapes and symbolic transitions of one env-mode assertion."""

    LIMIT = 100_000           # cached tree nodes before flushing the transitions

    def __init__(self, tmpl, nlocals, envfns, actfns):
        self.tmpl = tmpl
        self.nlocals = nlocals
        self.envfns, self.actfns = envfns, actfns
        self.ns = {f"Q{i}": f for i, f in enumerate(envfns)}
        self.ns.update({f"A{i}": f for i, f in enumerate(actfns)})
        self.shapes: list = []
        self.ids: dict = {}
        self.need: list = []
        self.trans: list = []      # sid -> {masked bits: node}
        self.nsl: list = []        # sid -> number of slots
        self.canon: dict = {}      # (raw sid, perm) -> sid
        self.nodes = 0
        shape = tmpl.init(slot(0))
        self.init_sid = self.intern(shape)
        self.init_envs = ((None,) * nlocals,) if senvs_of(shape, {}) else ()

    def intern(self, shape):
        sid = self.ids.get(shape)
        if sid is None:
            sid = self.ids[shape] = len(self.shapes)
            self.shapes.append(shape)
            self.need.append(self.tmpl.needs(shape))
            self.trans.append({})
            self.nsl.append(len(senvs_of(shape, {})))
        return sid

    def term(self, sid, envs):
        """The concrete term of an attempt."""
        return tmap(self.shapes[sid], lambda s: envs[s.i])

    # -- stepping -----------------------------------------------------------
    def step(self, sid, E, bits, args):
        """One tick of the attempt (sid, E): (verdict, next sid, next envs)."""
        tr = self.trans[sid]
        key = bits & self.need[sid]
        node = tr.get(key)
        if node is None:
            node = self._expand(sid, E, bits, args, {}, tr, key)
        while type(node) is list:              # [query fn, key, if false, if true, answers]
            r = node[0](E, *args)
            nx = node[3] if r else node[2]
            if nx is None:
                ans = dict(node[4])
                ans[node[1]] = bool(r)
                nx = self._expand(sid, E, bits, args, ans, node, 3 if r else 2)
            node = nx
        v, rsid, mk, ok = node
        if v is not None:
            return v, None, None
        E2 = E if mk is None else mk(E, *args)
        if ok:
            return None, rsid, E2
        rsid, E2 = self.canonical(rsid, E2)
        return None, rsid, E2

    def canonical(self, rsid, E2):
        """Raw next shape and its raw slots' envs -> canonical (sid, envs)."""
        d = sorted(set(E2), key=ekey)
        rank = {e: i for i, e in enumerate(d)}
        perm = tuple([rank[e] for e in E2])
        ck = (rsid, perm)
        sid2 = self.canon.get(ck)
        if sid2 is None:
            sid2 = self.canon[ck] = self.intern(tmap(self.shapes[rsid], lambda s: slot(perm[s.i])))
        return sid2, tuple(d)

    def _expand(self, sid, E, bits, args, answers, parent, where):
        """Step shape sid symbolically with the known answers; evaluate the
        other env conditions concretely (for E); attach the new path."""
        if self.nodes > self.LIMIT:
            self.flush()
            parent, where = self.trans[sid], bits & self.need[sid]
            answers = {}
        memo = {}
        envfns, actfns = self.envfns, self.actfns

        def conc(s):
            e = memo.get(s)
            if e is None:
                if type(s) is Slot:
                    e = E[s.i]
                else:
                    p = conc(s.parent)
                    lst = list(p)
                    for local, act in s.assigns:
                        lst[local] = actfns[act](*args, p)
                    e = tuple(lst)
                memo[s] = e
            return e

        val = SymVal(bits, dict(answers), lambda idx, s: envfns[idx](*args, conc(s)))
        v, t2 = self.tmpl.step(self.shapes[sid], val)
        node = self._leaf(v, t2, self.nsl[sid])
        known = dict(answers)
        chain = []
        for q, r in val.path:
            chain.append((self._query(q), q, dict(known), r))
            known[q] = r
        for qf, q, pre, r in reversed(chain):
            n = [qf, q, None, None, pre]
            n[3 if r else 2] = node
            node = n
        self.nodes += len(chain) + 1
        parent[where] = node
        return node

    def flush(self):
        self.trans = [{} for _ in self.shapes]
        self.nodes = 0

    def _query(self, q):
        idx, s = q
        g = _Gen(self.nlocals)
        e = g.env(s)
        src = "\n".join(["def _q(E, c, R, cx, RX, H):"] + g.lines
                        + [f"    return Q{idx}(c, R, cx, RX, H, {e})"])
        return compile_fn(src, "_q", self.ns)

    def _leaf(self, v, t2, nsrc):
        """(verdict, raw next sid, envs function or None, canonical as is)."""
        if v is not None:
            return (v, None, None, True)
        found = senvs_of(t2, {})
        slots = sorted((s for s in found if type(s) is Slot), key=lambda s: s.i)
        order = slots + [s for s in found if type(s) is not Slot]
        num = {s: k for k, s in enumerate(order)}
        rsid = self.intern(tmap(t2, lambda s: slot(num[s])))
        derived = len(order) > len(slots)
        if not derived and all(s.i == k for k, s in enumerate(slots)) and len(slots) == nsrc:
            mk = None
        else:
            g = _Gen(self.nlocals)
            names = [g.env(s) for s in order]
            src = "\n".join(["def _mk(E, c, R, cx, RX, H):"] + g.lines
                            + [f"    return ({''.join(n + ', ' for n in names)})"])
            mk = compile_fn(src, "_mk", self.ns)
        # kept slots of a sorted distinct list stay sorted and distinct
        return (None, rsid, mk, not derived or len(order) <= 1)


def compile_fn(src, name, ns):
    g = dict(ns)
    exec(compile(src, f"<sva:{name}>", "exec"), g)
    return g[name]
