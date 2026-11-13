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

PASS, VPASS, FAIL = 1, 2, 3

_EMPTY = frozenset()


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


class ImplT(Template):
    """S |-> P (overlap) and S |=> P. Term: (antecedent threads, matched, nonvacuous, consequents)."""

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
        return (a, False, False, _EMPTY)

    def step(self, term, val):
        A, matched, nonvac, C = term
        cons = self.cons
        newc = set()
        for c in C:
            v, c2 = cons.step(c, val)
            if v is None:
                newc.add(c2)
            elif v == FAIL:
                return FAIL, None
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
                    elif v == FAIL:
                        return FAIL, None
                    elif v == PASS:
                        nonvac = True
                else:
                    newc.add(c0)
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
            m |= self.cons.needs(c)
        return m

    def finish(self, term):
        return all(self.cons.finish(c) for c in term[3])


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
        return (PASS if v == FAIL else FAIL), None

    def needs(self, term):
        return self.p.needs(term)

    def finish(self, term):
        return not self.p.finish(term)


_DONE_P, _DONE_V, _DONE_F = ("#P",), ("#V",), ("#F",)


class AndT(Template):
    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    def _side(self, tmpl, t, val):
        if type(t) is tuple and t in (_DONE_P, _DONE_V):
            return t
        v, t2 = tmpl.step(t, val)
        if v is None:
            return t2
        return {PASS: _DONE_P, VPASS: _DONE_V, FAIL: _DONE_F}[v]

    def step(self, term, val):
        a = self._side(self.p, term[0], val)
        b = self._side(self.q, term[1], val)
        if a is _DONE_F or b is _DONE_F:
            return FAIL, None
        if a in (_DONE_P, _DONE_V) and b in (_DONE_P, _DONE_V):
            return (PASS if _DONE_P in (a, b) else VPASS), None
        return None, (a, b)

    def needs(self, term):
        m = 0
        if term[0] not in (_DONE_P, _DONE_V):
            m |= self.p.needs(term[0])
        if term[1] not in (_DONE_P, _DONE_V):
            m |= self.q.needs(term[1])
        return m

    def finish(self, term):
        a = term[0] in (_DONE_P, _DONE_V) or self.p.finish(term[0])
        b = term[1] in (_DONE_P, _DONE_V) or self.q.finish(term[1])
        return a and b


class OrT(Template):
    def __init__(self, p: Template, q: Template):
        self.p, self.q = p, q
        self.envmode = p.envmode or q.envmode
        self.init_need = p.init_need | q.init_need

    def init(self, env):
        return (self.p.init(env), self.q.init(env))

    def step(self, term, val):
        res = []
        nxt = []
        for tmpl, t in ((self.p, term[0]), (self.q, term[1])):
            if t is _DONE_F:
                res.append(FAIL)
                nxt.append(t)
                continue
            v, t2 = tmpl.step(t, val)
            res.append(v)
            nxt.append(_DONE_F if v == FAIL else t2)
        if PASS in res:
            return PASS, None
        if VPASS in res:
            return VPASS, None
        if res[0] == FAIL and res[1] == FAIL:
            return FAIL, None
        return None, (nxt[0], nxt[1])

    def needs(self, term):
        m = 0
        if term[0] is not _DONE_F:
            m |= self.p.needs(term[0])
        if term[1] is not _DONE_F:
            m |= self.q.needs(term[1])
        return m

    def finish(self, term):
        a = term[0] is not _DONE_F and self.p.finish(term[0])
        b = term[1] is not _DONE_F and self.q.finish(term[1])
        return a or b


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
