"""Property templates: evaluation of one assertion attempt by progression.

An attempt's state is a hashable *term*. `step(term, val)` consumes one clock
tick and returns (verdict, next_term); a verdict ends the attempt:

  PASS   nonvacuous success        VPASS  vacuous success        FAIL  failure

`needs(term)` is the set of base atoms (bit mask) the next step reads, which
drives demand-driven sampling. `finish(term)` gives the truth value of a
still-pending attempt when the trace ends (weak obligations hold, strong ones
fail).

Env-free properties use plain NFA state sets; properties with local variables
("env mode") use threads (state, env) where env is a tuple of local values.
"""
from __future__ import annotations

PASS, VPASS, FAIL, VFAIL = 1, 2, 3, 5
# Vacuity (IEEE 1800 16.14.8) is a property of the evaluation, not of the
# outcome: VFAIL is a failure whose evaluation was vacuous (e.g. `not` of a
# vacuous success). Both FAIL and VFAIL are reported as failures.
TRUE_V = (PASS, VPASS)

_EMPTY = frozenset()


class _Unstarted:
    """Marks a consequent spawned by |=> / #=# that has not seen its first tick."""
    __slots__ = ("t",)

    def __init__(self, t):
        self.t = t

    def __eq__(self, other):
        return type(other) is _Unstarted and other.t == self.t

    def __hash__(self):
        return hash(("#U", self.t))

    def __repr__(self):
        return f"Unstarted({self.t!r})"


class _Pre(_Unstarted):
    """A |=> / #=# consequent from an empty antecedent match: it becomes
    _Unstarted after the start tick, so its first tick is the next one."""
    __slots__ = ()

    def __eq__(self, other):
        return type(other) is _Pre and other.t == self.t

    def __hash__(self):
        return hash(("#PRE", self.t))


class Val:
    """Valuation of one tick: base-atom bits plus (env mode) a context that
    evaluates env atoms and assignment actions."""
    __slots__ = ("bits", "ctx")

    def __init__(self, bits: int, ctx=None):
        self.bits = bits
        self.ctx = ctx

    def env_atom(self, idx, env):
        return self.ctx.env_atom(idx, env)

    def action(self, idx, env):
        return self.ctx.action(idx, env)


def guard_true(g, val: Val, env) -> bool:
    pos, neg, envlits = g
    bits = val.bits
    if (bits & pos) != pos or (bits & neg):
        return False
    for idx, pol in envlits:
        if bool(val.env_atom(idx, env)) != pol:
            return False
    return True


class Template:
    envmode = False
    init_need = 0

    def init(self, env):
        raise NotImplementedError

    def step(self, term, val):
        raise NotImplementedError

    def needs(self, term) -> int:
        raise NotImplementedError

    def finish(self, term) -> bool:
        raise NotImplementedError

    def nv(self, term) -> bool:
        """True if this pending evaluation is already known to be nonvacuous."""
        return False

    def fin0(self) -> bool:
        """End-of-test truth of an evaluation that has not seen any tick (the
        consequent of |=> spawned at the last tick): only strong obligations
        fail."""
        return True


class SeqT(Template):
    """A sequence used as a property: passes at its first match."""

    def __init__(self, nfa, strong: bool, envmode: bool):
        self.nfa = nfa
        self.strong = strong
        self.envmode = envmode
        self.init_need = nfa.needs(nfa.init)
        self._init_free = nfa.init

    def init(self, env):
        if self.envmode:
            return frozenset((q, env) for q in self.nfa.init)
        return self._init_free

    def step(self, term, val):
        if self.envmode:
            nxt, matches = self.nfa.step_env(term, val)
            matched = bool(matches)
        else:
            nxt, matched = self.nfa.step(term, val.bits)
        if matched:
            return PASS, None
        if not nxt:
            return FAIL, None
        return None, nxt

    def needs(self, term):
        return self.nfa.needs(term)

    def finish(self, term):
        return not self.strong

    def fin0(self):
        return not self.strong

    def nv(self, term):
        return True


class ImplT(Template):
    """S |-> P (overlap) and S |=> P. Term: (antecedent threads, matched, nonvacuous, consequents).

    For |=> a consequent spawned at the tick that ends the antecedent match is
    kept as ("#U", term) until its first tick: at the end of the test such an
    unstarted obligation is not evaluated (reference-simulator behaviour)."""

    def __init__(self, ante, cons: Template, overlap: bool, envmode: bool):
        self.ante = ante
        self.cons = cons
        self.overlap = overlap
        self.envmode = envmode
        self.init_need = ante.needs(ante.init) | (cons.init_need if overlap else 0)

    def init(self, env):
        if self.envmode:
            a = frozenset((q, env) for q in self.ante.init)
        else:
            a = self.ante.init
        if not self.overlap and self.ante.nullable:
            # an empty match of the antecedent of |=> ends at the start tick:
            # the consequent starts at the next tick (reference behaviour)
            return (a, True, False, frozenset((_Pre(self.cons.init(env)),)))
        return (a, False, False, _EMPTY)

    def step(self, term, val):
        A, matched, nonvac, C = term
        cons = self.cons
        newc = set()
        for c in C:
            if type(c) is _Pre:
                newc.add(_Unstarted(c.t))
                continue
            if type(c) is _Unstarted:
                c = c.t
            v, c2 = cons.step(c, val)
            if v is None:
                newc.add(c2)
            elif v == FAIL or v == VFAIL:
                return v, None
            elif v == PASS:
                nonvac = True
        if A:
            if self.envmode:
                A2, envs = self.ante.step_env(A, val)
            else:
                A2, m = self.ante.step(A, val.bits)
                envs = ((),) if m else ()
            for env in envs:
                matched = True
                c0 = cons.init(env)
                if self.overlap:
                    v, c2 = cons.step(c0, val)
                    if v is None:
                        newc.add(c2)
                    elif v == FAIL or v == VFAIL:
                        return v, None
                    elif v == PASS:
                        nonvac = True
                else:
                    newc.add(_Unstarted(c0))
        else:
            A2 = A
        if not A2 and not newc:
            return (PASS if (matched and nonvac) else VPASS), None
        return None, (A2, matched, nonvac, frozenset(newc))

    def needs(self, term):
        A, _, _, C = term
        m = 0
        if A:
            m = self.ante.needs(A)
            if self.overlap:
                m |= self.cons.init_need
        for c in C:
            if type(c) is _Unstarted:
                m |= self.cons.init_need
            elif type(c) is not _Pre:
                m |= self.cons.needs(c)
        return m

    def finish(self, term):
        cons = self.cons
        return all(cons.fin0() if isinstance(c, _Unstarted) else cons.finish(c) for c in term[3])

    def nv(self, term):
        A, matched, nonvac, C = term
        if nonvac:
            return True
        return any(self.cons.nv(c.t if isinstance(c, _Unstarted) else c) for c in C)


class NotT(Template):
    def __init__(self, p: Template):
        self.p = p
        self.envmode = p.envmode
        self.init_need = p.init_need

    def init(self, env):
        return self.p.init(env)

    def step(self, term, val):
        v, t = self.p.step(term, val)
        if v is None:
            return None, t
        return {PASS: FAIL, VPASS: VFAIL, FAIL: PASS, VFAIL: VPASS}[v], None

    def needs(self, term):
        return self.p.needs(term)

    def finish(self, term):
        return not self.p.finish(term)

    def nv(self, term):
        return self.p.nv(term)


_DONE_P, _DONE_V, _DONE_F, _DONE_VF = ("#P",), ("#V",), ("#F",), ("#VF",)
_DONE = {PASS: _DONE_P, VPASS: _DONE_V, FAIL: _DONE_F, VFAIL: _DONE_VF}
_UNDONE = {_DONE_P: PASS, _DONE_V: VPASS, _DONE_F: FAIL, _DONE_VF: VFAIL}


def _is_done(t):
    return t is _DONE_P or t is _DONE_V or t is _DONE_F or t is _DONE_VF


def _side_nv(tmpl, t) -> bool:
    if t is _DONE_P or t is _DONE_F:
        return True
    if t is _DONE_V or t is _DONE_VF:
        return False
    return tmpl.nv(t)


def _verdict(truth: bool, nonvac: bool) -> int:
    if truth:
        return PASS if nonvac else VPASS
    return FAIL if nonvac else VFAIL


class AndT(Template):
    """P and Q: fails as soon as either side fails; passes when both pass.
    Nonvacuous if either side's evaluation is nonvacuous."""

    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    @staticmethod
    def _adv(tmpl, t, val):
        if _is_done(t):
            return t
        v, t2 = tmpl.step(t, val)
        return t2 if v is None else _DONE[v]

    def step(self, term, val):
        a = self._adv(self.p, term[0], val)
        b = self._adv(self.q, term[1], val)
        for x, y in ((a, b), (b, a)):
            if x is _DONE_F:
                return FAIL, None
            if x is _DONE_VF:
                return (FAIL if (y is _DONE_P or y is _DONE_F) else VFAIL), None
        if _is_done(a) and _is_done(b):
            return (PASS if (a is _DONE_P or b is _DONE_P) else VPASS), None
        return None, (a, b)

    def needs(self, term):
        m = 0
        if not _is_done(term[0]):
            m |= self.p.needs(term[0])
        if not _is_done(term[1]):
            m |= self.q.needs(term[1])
        return m

    def finish(self, term):
        a = term[0] in (_DONE_P, _DONE_V) or (not _is_done(term[0]) and self.p.finish(term[0]))
        b = term[1] in (_DONE_P, _DONE_V) or (not _is_done(term[1]) and self.q.finish(term[1]))
        return a and b

    def nv(self, term):
        return _side_nv(self.p, term[0]) or _side_nv(self.q, term[1])

    def fin0(self):
        return self.p.fin0() and self.q.fin0()


class OrT(Template):
    """P or Q: a nonvacuous success of either side decides at once. Otherwise
    the result waits for both sides: true if either is true, nonvacuous if
    either evaluation is nonvacuous (IEEE 1800 16.14.8)."""

    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    _adv = staticmethod(AndT._adv)

    def step(self, term, val):
        a = self._adv(self.p, term[0], val)
        b = self._adv(self.q, term[1], val)
        if a is _DONE_P or b is _DONE_P:
            return PASS, None
        if _is_done(a) and _is_done(b):
            truth = a is _DONE_V or b is _DONE_V
            nonvac = a is _DONE_F or b is _DONE_F
            return _verdict(truth, nonvac), None
        # one side vacuously true: the result is true; it is nonvacuous as soon
        # as the other (pending) evaluation is known to be nonvacuous
        if a is _DONE_V and self.q.nv(b):
            return PASS, None
        if b is _DONE_V and self.p.nv(a):
            return PASS, None
        return None, (a, b)

    def nv(self, term):
        return _side_nv(self.p, term[0]) or _side_nv(self.q, term[1])

    def fin0(self):
        return self.p.fin0() or self.q.fin0()

    def needs(self, term):
        m = 0
        if not _is_done(term[0]):
            m |= self.p.needs(term[0])
        if not _is_done(term[1]):
            m |= self.q.needs(term[1])
        return m

    def finish(self, term):
        a = term[0] is _DONE_V or (not _is_done(term[0]) and self.p.finish(term[0]))
        b = term[1] is _DONE_V or (not _is_done(term[1]) and self.q.finish(term[1]))
        return a or b


class IffT(Template):
    """P iff Q: true when both have the same truth value; nonvacuous if either
    evaluation is nonvacuous. Decided when both sides are done."""

    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    _adv = staticmethod(AndT._adv)

    def step(self, term, val):
        a = self._adv(self.p, term[0], val)
        b = self._adv(self.q, term[1], val)
        if _is_done(a) and _is_done(b):
            ta, tb = a in (_DONE_P, _DONE_V), b in (_DONE_P, _DONE_V)
            nonvac = a in (_DONE_P, _DONE_F) or b in (_DONE_P, _DONE_F)
            return _verdict(ta == tb, nonvac), None
        return None, (a, b)

    needs = OrT.needs
    nv = OrT.nv

    def finish(self, term):
        def truth(t, tmpl):
            return t in (_DONE_P, _DONE_V) or (not _is_done(t) and tmpl.finish(t))
        return truth(term[0], self.p) == truth(term[1], self.q)


class ImpliesT(Template):
    """P implies Q: vacuous when P is false; nonvacuous when P and Q hold
    nonvacuously. Both sides are evaluated in parallel from the same tick."""

    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    @staticmethod
    def _adv(tmpl, t, val):
        if _is_done(t):
            return t
        v, t2 = tmpl.step(t, val)
        return t2 if v is None else _DONE[v]

    def step(self, term, val):
        a = self._adv(self.p, term[0], val)
        b = self._adv(self.q, term[1], val)
        if a is _DONE_F or a is _DONE_VF:
            return VPASS, None                          # antecedent false: vacuous
        if a is _DONE_P or a is _DONE_V:
            if b is _DONE_F or b is _DONE_VF:
                return FAIL, None
            if b is _DONE_P or b is _DONE_V:
                return (PASS if (a is _DONE_P and b is _DONE_P) else VPASS), None
        return None, (a, b)

    def needs(self, term):
        m = 0
        if not _is_done(term[0]):
            m |= self.p.needs(term[0])
        if not _is_done(term[1]):
            m |= self.q.needs(term[1])
        return m

    def finish(self, term):
        a, b = term
        pa = a in (_DONE_P, _DONE_V) or (not _is_done(a) and self.p.finish(a))
        if not pa:
            return True
        return b in (_DONE_P, _DONE_V) or (not _is_done(b) and self.q.finish(b))


class FollowedByT(Template):
    """S #-# P / S #=# P: some match of S must be followed by P holding.

    Passes when one P instance passes (vacuously if that instance was vacuous);
    fails when S can no longer match and every P instance failed. At the end
    of the trace an antecedent that can still match is not a failure."""

    def __init__(self, ante, cons: Template, overlap: bool, envmode: bool):
        self.ante = ante
        self.cons = cons
        self.overlap = overlap
        self.envmode = envmode
        self.init_need = ante.needs(ante.init) | (cons.init_need if overlap else 0)

    def init(self, env):
        a = frozenset((q, env) for q in self.ante.init) if self.envmode else self.ante.init
        if not self.overlap and self.ante.nullable:
            return (a, frozenset((_Pre(self.cons.init(env)),)))
        return (a, _EMPTY)

    def step(self, term, val):
        A, C = term
        cons = self.cons
        newc = set()
        vac = False
        for c in C:
            if type(c) is _Pre:
                newc.add(_Unstarted(c.t))
                continue
            if type(c) is _Unstarted:
                c = c.t
            v, c2 = cons.step(c, val)
            if v is None:
                newc.add(c2)
            elif v == PASS:
                return PASS, None
            elif v == VPASS:
                vac = True
        if A:
            if self.envmode:
                A2, envs = self.ante.step_env(A, val)
            else:
                A2, m = self.ante.step(A, val.bits)
                envs = ((),) if m else ()
            for env in envs:
                c0 = cons.init(env)
                if self.overlap:
                    v, c2 = cons.step(c0, val)
                    if v is None:
                        newc.add(c2)
                    elif v == PASS:
                        return PASS, None
                    elif v == VPASS:
                        vac = True
                else:
                    newc.add(_Unstarted(c0))
        else:
            A2 = A
        if vac:
            return VPASS, None
        if not A2 and not newc:
            return FAIL, None
        return None, (A2, frozenset(newc))

    def needs(self, term):
        A, C = term
        m = 0
        if A:
            m = self.ante.needs(A)
            if self.overlap:
                m |= self.cons.init_need
        for c in C:
            if type(c) is _Unstarted:
                m |= self.cons.init_need
            elif type(c) is not _Pre:
                m |= self.cons.needs(c)
        return m

    def finish(self, term):
        A, C = term
        return bool(A) or any(isinstance(c, _Unstarted) or self.cons.finish(c) for c in C)

    def nv(self, term):
        return any(self.cons.nv(c.t if isinstance(c, _Unstarted) else c) for c in term[1])


class IfT(Template):
    """if (cond) P [else Q]: cond is a guard (pos, neg, envlits) at the first tick."""

    def __init__(self, guard, p: Template, q: Template | None, envmode: bool):
        self.guard = guard
        self.p, self.q = p, q
        self.envmode = envmode
        self.init_need = guard[0] | guard[1] | p.init_need | (q.init_need if q else 0)

    def init(self, env):
        return ("?", env)

    def step(self, term, val):
        if term[0] == "?":
            env = term[1]
            if guard_true(self.guard, val, env):
                branch, tmpl = 0, self.p
            elif self.q is not None:
                branch, tmpl = 1, self.q
            else:
                return VPASS, None
            t = tmpl.init(env)
        else:
            branch, t = term
            tmpl = self.p if branch == 0 else self.q
        v, t2 = tmpl.step(t, val)
        if v is not None:
            return v, None
        return None, (branch, t2)

    def needs(self, term):
        if term[0] == "?":
            return self.init_need
        return (self.p if term[0] == 0 else self.q).needs(term[1])

    def finish(self, term):
        if term[0] == "?":
            return True
        return (self.p if term[0] == 0 else self.q).finish(term[1])

    def nv(self, term):
        if term[0] == "?":
            return False
        return (self.p if term[0] == 0 else self.q).nv(term[1])


class ConstT(Template):
    """Resolves at the first tick with a fixed verdict."""

    def __init__(self, verdict: int, envmode: bool = False):
        self.verdict = verdict
        self.envmode = envmode

    def init(self, env):
        return ("const",)

    def step(self, term, val):
        return self.verdict, None

    def needs(self, term):
        return 0

    def finish(self, term):
        return self.verdict != FAIL

    def fin0(self):
        return self.verdict != FAIL


# --------------------------------------------------------------------------
# Temporal property operators (IEEE 1800 16.12.10 - 16.12.14)
# --------------------------------------------------------------------------

def _adv(tmpl, t, val):
    if _is_done(t):
        return t
    v, t2 = tmpl.step(t, val)
    return t2 if v is None else _DONE[v]


def _true_done(t):
    return t is _DONE_P or t is _DONE_V


def _false_done(t):
    return t is _DONE_F or t is _DONE_VF


class NextT(Template):
    """nexttime[n] P / s_nexttime[n] P (n >= 1): P is evaluated from the n-th
    next tick. Term: ("N", k, env) while k more ticks are skipped before P
    starts, then ("P", term of P)."""

    def __init__(self, p: Template, n: int, strong: bool, envmode: bool):
        self.p, self.n, self.strong = p, n, strong
        self.envmode = envmode
        self.init_need = 0

    def init(self, env):
        return ("N", self.n, env)

    def step(self, term, val):
        if term[0] == "N":
            if term[1] > 0:
                return None, ("N", term[1] - 1, term[2])
            t = self.p.init(term[2])
        else:
            t = term[1]
        v, t2 = self.p.step(t, val)
        if v is not None:
            return v, None
        return None, ("P", t2)

    def needs(self, term):
        if term[0] == "N":
            return self.p.init_need if term[1] == 0 else 0
        return self.p.needs(term[1])

    def finish(self, term):
        if term[0] == "N":
            return not self.strong
        return self.p.finish(term[1])

    def nv(self, term):
        return term[0] == "P" and self.p.nv(term[1])

    def fin0(self):
        return not self.strong


class AlwaysT(Template):
    """always [lo:hi] P / s_always [lo:hi] P (hi None means $, weak form only).

    A new evaluation of P starts at every tick of the range; all must hold.
    Term: (k, nonvac, instances, env), k = ticks consumed (saturated at lo when
    the range is unbounded, so the term space stays finite)."""

    def __init__(self, p: Template, lo: int, hi, strong: bool, envmode: bool):
        self.p, self.lo, self.hi, self.strong = p, lo, hi, strong
        self.envmode = envmode
        self.init_need = p.init_need if lo == 0 else 0

    def init(self, env):
        return (0, False, _EMPTY, env)

    def _in_range(self, k):
        return k >= self.lo and (self.hi is None or k <= self.hi)

    def step(self, term, val):
        k, nonvac, insts, env = term
        p = self.p
        live = set()
        todo = list(insts)
        if self._in_range(k):
            todo.append(p.init(env))
        for t in todo:
            v, t2 = p.step(t, val)
            if v is None:
                live.add(t2)
            elif v == FAIL or v == VFAIL:
                return v, None
            elif v == PASS:
                nonvac = True
        if self.hi is not None and k >= self.hi and not live:
            return PASS, None       # reference: never vacuous
        k2 = k + 1 if (self.hi is not None or k < self.lo) else k
        return None, (k2, nonvac, frozenset(live), env)

    def needs(self, term):
        k, _, insts, _ = term
        m = self.p.init_need if self._in_range(k) else 0
        for t in insts:
            m |= self.p.needs(t)
        return m

    def finish(self, term):
        k, _, insts, _ = term
        if self.strong and self.hi is not None and k <= self.hi:
            return False
        return all(self.p.finish(t) for t in insts)

    def nv(self, term):
        return True                 # reference: always/eventually are never vacuous

    def fin0(self):
        return not self.strong


class EventuallyT(Template):
    """eventually [lo:hi] P (bounded, weak) / s_eventually [lo:hi] P (hi None
    means $): a new evaluation of P starts at every tick of the range; one
    must hold. Term: (k, instances, env)."""

    def __init__(self, p: Template, lo: int, hi, strong: bool, envmode: bool):
        self.p, self.lo, self.hi, self.strong = p, lo, hi, strong
        self.envmode = envmode
        self.init_need = p.init_need if lo == 0 else 0

    def init(self, env):
        return (0, False, _EMPTY, env)

    _in_range = AlwaysT._in_range

    def step(self, term, val):
        k, vac, insts, env = term
        p = self.p
        live = set()
        todo = list(insts)
        if self._in_range(k):
            todo.append(p.init(env))
        for t in todo:
            v, t2 = p.step(t, val)
            if v is None:
                live.add(t2)
            elif v == PASS or v == VPASS:
                return PASS, None   # reference: any success decides, never vacuous
        if self.hi is not None and k >= self.hi and not live:
            return FAIL, None
        k2 = k + 1 if (self.hi is not None or k < self.lo) else k
        return None, (k2, vac, frozenset(live), env)

    def needs(self, term):
        k, _, insts, _ = term
        m = self.p.init_need if self._in_range(k) else 0
        for t in insts:
            m |= self.p.needs(t)
        return m

    def finish(self, term):
        k, vac, insts, _ = term
        if vac or any(self.p.finish(t) for t in insts):
            return True
        if self.hi is None or k <= self.hi:
            return not self.strong
        return False

    def nv(self, term):
        return True

    def fin0(self):
        return not self.strong


class UntilT(Template):
    """P until Q, P s_until Q, P until_with Q, P s_until_with Q.

    Holds if Q holds at some tick k and P holds at every tick before k (until)
    or up to and including k (until_with); the weak forms also hold if P
    holds at every tick. P and Q are general properties, so an evaluation of
    each starts at every tick until the outcome is known.

    Term: (closed, entries, nonvac, env). entries[j] = [P term, Q term] of the
    j-th tick still relevant (done markers once resolved). `closed`: no more
    ticks are needed (a P failed, or a candidate Q matched)."""

    def __init__(self, p: Template, q: Template, with_: bool, strong: bool, envmode: bool):
        self.p, self.q, self.with_, self.strong = p, q, with_, strong
        self.envmode = envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (False, (), False, env)

    def _req(self, k):
        """Number of leading entries whose P candidate k requires."""
        return k + 1 if self.with_ else k

    def _alive(self, ents, k):
        return not _false_done(ents[k][1]) and not any(_false_done(pt) for pt, _ in ents[:self._req(k)])

    def step(self, term, val):
        closed, entries, nonvac, env = term
        p, q = self.p, self.q
        ents = [[_adv(p, pt, val), _adv(q, qt, val)] for pt, qt in entries]
        if not closed:
            ents.append([_adv(p, p.init(env), val), _adv(q, q.init(env), val)])
        # a failed P kills every candidate that requires it; no more ticks needed
        for j, (pt, _) in enumerate(ents):
            if _false_done(pt):
                closed = True
                del ents[(j if self.with_ else j + 1):]
                break
        for k in range(len(ents)):
            if not self._alive(ents, k) or not _true_done(ents[k][1]):
                continue
            req = ents[:self._req(k)]
            if all(_true_done(pt) for pt, _ in req):
                nv = nonvac or ents[k][1] is _DONE_P or any(pt is _DONE_P for pt, _ in req)
                return (PASS if nv else VPASS), None
            # Q matched, some required P pending: later candidates are dominated
            closed = True
            del ents[k + 1:]
            if not self.with_ and not _is_done(ents[k][0]):
                ents[k][0] = _DONE_V        # not required by this candidate
            break
        if closed and not any(self._alive(ents, k) for k in range(len(ents))):
            return FAIL, None
        # leading entries whose P held and whose Q failed no longer matter
        while ents and _true_done(ents[0][0]) and _false_done(ents[0][1]):
            nonvac = nonvac or ents[0][0] is _DONE_P
            del ents[0]
        return None, (closed, tuple(map(tuple, ents)), nonvac, env)

    def needs(self, term):
        closed, entries, _, _ = term
        m = 0 if closed else self.init_need
        for pt, qt in entries:
            if not _is_done(pt):
                m |= self.p.needs(pt)
            if not _is_done(qt):
                m |= self.q.needs(qt)
        return m

    @staticmethod
    def _ftruth(tmpl, t):
        return _true_done(t) if _is_done(t) else tmpl.finish(t)

    def finish(self, term):
        closed, entries, _, _ = term
        for k, (_, qt) in enumerate(entries):
            if self._ftruth(self.q, qt) and all(self._ftruth(self.p, pt)
                                                for pt, _ in entries[:self._req(k)]):
                return True
        if self.strong or closed:
            return False
        return all(self._ftruth(self.p, pt) for pt, _ in entries)

    def fin0(self):
        return not self.strong

    def nv(self, term):
        closed, entries, nonvac, _ = term
        if nonvac:
            return True
        for pt, qt in entries:
            if pt is _DONE_P or qt is _DONE_P:
                return True
            if not _is_done(pt) and self.p.nv(pt):
                return True
            if not _is_done(qt) and self.q.nv(qt):
                return True
        return False


class AbortT(Template):
    """accept_on / reject_on and their sync_ forms (identical on sampled ticks).

    The condition is checked at every tick of the evaluation, the first one
    included. When it is true the evaluation ends with a success (accept) or a
    failure (reject), whatever the operand's own verdict at that tick, except
    that a vacuous success of the operand stays vacuous (reference
    behaviour)."""

    def __init__(self, guard, p: Template, accept: bool):
        self.guard, self.p, self.accept = guard, p, accept
        self.envmode = p.envmode
        self.gmask = guard[0] | guard[1]
        self.init_need = self.gmask | p.init_need

    def init(self, env):
        return self.p.init(env)

    def step(self, term, val):
        v, t2 = self.p.step(term, val)
        if v != VPASS and guard_true(self.guard, val, None):
            return (PASS if self.accept else FAIL), None
        return v, t2

    def needs(self, term):
        return self.gmask | self.p.needs(term)

    def finish(self, term):
        return self.p.finish(term)

    def fin0(self):
        return self.p.fin0()

    def nv(self, term):
        return self.p.nv(term)
