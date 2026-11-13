"""Tick automata for SVA sequences.

Every transition consumes exactly one clock tick. A fragment is
(I, F, nullable): I = states before the first tick, F = states reached by a
transition that ends a match, nullable = the sequence admits the empty match.
Initial states are never transition targets, which keeps ##1 concatenation
and ##0 fusion simple:

  S1 ##1 S2 : from every final state of S1, add copies of S2's first transitions.
  S1 ##0 S2 : every transition entering a final state of S1 is fused (guards
              conjoined, actions kept in order) with every first transition of S2.

Transition labels are tuples of ops, evaluated in order:
  (0, pos, neg, envlits)  guard: base atoms in pos must be true, in neg false;
                          envlits = ((env atom index, polarity), ...)
  (1, assigns)            local variable assignments ((local, action index), ...)
"""
from __future__ import annotations

from itertools import product

from ._errors import SvaUnsupportedError

TRUE_GUARD = (0, 0, 0, ())


def merge_ops(a: tuple, b: tuple):
    """Concatenate two op tuples, merging adjacent guards.

    Contradictory guards (an atom required both true and false) are kept: the
    transition then simply never fires. Dropping it at build time would make
    attempts fail one tick *before* the tick that actually decides them, which
    differs from the formal semantics and from simulators.
    """
    ops = list(a)
    for op in b:
        if op[0] == 0 and ops and ops[-1][0] == 0:
            g = ops[-1]
            ops[-1] = (0, g[1] | op[1], g[2] | op[2], g[3] + op[3])
        else:
            ops.append(op)
    return tuple(ops)


class Frag:
    __slots__ = ("I", "F", "nullable")

    def __init__(self, I, F, nullable):
        self.I = list(I)
        self.F = set(F)
        self.nullable = nullable


class Builder:
    def __init__(self):
        self.trans: list[list] = []      # state -> [(ops, dest)]

    def new(self) -> int:
        self.trans.append([])
        return len(self.trans) - 1

    def add(self, src: int, ops: tuple, dest: int):
        if ops is not None:
            self.trans[src].append((ops, dest))

    # -- primitives ---------------------------------------------------------
    def guard(self, pos=0, neg=0, envlits=()) -> Frag:
        s, f = self.new(), self.new()
        self.add(s, ((0, pos, neg, tuple(envlits)),), f)
        return Frag([s], [f], False)

    def true(self) -> Frag:
        return self.guard()

    def empty(self) -> Frag:
        return Frag([self.new()], [], True)

    def never(self) -> Frag:
        return Frag([self.new()], [], False)

    # -- composition --------------------------------------------------------
    def _first_transitions(self, f: Frag):
        for i in f.I:
            yield from self.trans[i]

    def concat(self, a: Frag, b: Frag) -> Frag:
        """a ##1 b"""
        firsts = list(self._first_transitions(b))
        for fs in a.F:
            for ops, dest in firsts:
                self.add(fs, ops, dest)
        I = a.I + (b.I if a.nullable else [])
        F = set(b.F) | (a.F if b.nullable else set())
        return Frag(I, F, a.nullable and b.nullable)

    def fuse(self, a: Frag, b: Frag) -> Frag:
        """a ##0 b (empty matches of either side do not take part)."""
        firsts = list(self._first_transitions(b))
        for src in range(len(self.trans)):
            for ops, dest in list(self.trans[src]):
                if dest in a.F:
                    for ops2, dest2 in firsts:
                        self.add(src, merge_ops(ops, ops2), dest2)
        return Frag(a.I, set(b.F), False)

    def union(self, a: Frag, b: Frag) -> Frag:
        return Frag(a.I + b.I, a.F | b.F, a.nullable or b.nullable)

    def optional(self, a: Frag) -> Frag:
        return Frag(a.I, a.F, True)

    def star(self, a: Frag) -> Frag:
        """a[*0:$]"""
        firsts = list(self._first_transitions(a))
        for fs in a.F:
            for ops, dest in firsts:
                self.add(fs, ops, dest)
        return Frag(a.I, a.F, True)

    def with_actions(self, a: Frag, assigns: tuple) -> Frag:
        """(a, v = e, ...): assignments run on the tick that ends a match of a.

        Final states may continue (longer matches), so the assignment goes on a
        copy of every final-entering transition leading to a new pure final.
        """
        if a.nullable:
            raise SvaUnsupportedError("local variable assignment on a sequence that can match empty")
        new_final = self.new()
        for src in range(len(self.trans)):
            for ops, dest in list(self.trans[src]):
                if dest in a.F:
                    self.add(src, ops + ((1, assigns),), new_final)
        return Frag(a.I, {new_final}, False)

    def _product(self, a: Frag, b: Frag, keep_waiting: bool) -> Frag:
        """Shared product construction for `and` (keep_waiting) and `intersect`."""
        TOP = -1
        index: dict = {}
        final = self.new()
        F = {final} if keep_waiting else set()
        work = []

        def state(pair):
            st = index.get(pair)
            if st is None:
                st = index[pair] = self.new()
                work.append(pair)
                if not keep_waiting and pair[0] in a.F and pair[1] in b.F:
                    F.add(st)
            return st

        I = []
        ia = list(a.I) + ([TOP] if keep_waiting and a.nullable else [])
        ib = list(b.I) + ([TOP] if keep_waiting and b.nullable else [])
        for x in ia:
            for y in ib:
                if (x, y) != (TOP, TOP):
                    I.append(state((x, y)))
        while work:
            x, y = pair = work.pop()
            src = index[pair]
            mx = self.trans[x] if x != TOP else [((), TOP)]
            my = self.trans[y] if y != TOP else [((), TOP)]
            for (ox, dx), (oy, dy) in product(mx, my):
                if dx == TOP and dy == TOP:
                    continue
                ops = merge_ops(ox, oy)
                if not keep_waiting:
                    self.add(src, ops, state((dx, dy)))
                    continue
                vx = [dx] if dx == TOP else ([dx, TOP] if dx in a.F else [dx])
                vy = [dy] if dy == TOP else ([dy, TOP] if dy in b.F else [dy])
                for p in product(vx, vy):
                    self.add(src, ops, final if p == (TOP, TOP) else state(p))
        return Frag(I, F, a.nullable and b.nullable)

    def and_(self, a: Frag, b: Frag) -> Frag:
        return self._product(a, b, keep_waiting=True)

    def intersect(self, a: Frag, b: Frag) -> Frag:
        return self._product(a, b, keep_waiting=False)

    def first_match(self, a: Frag, max_atoms: int = 16) -> Frag:
        """Deterministic sub-automaton that stops at the earliest match."""
        if a.nullable:
            return self.empty()
        final = self.new()
        index: dict = {}
        work = []

        def state(d):
            st = index.get(d)
            if st is None:
                st = index[d] = self.new()
                work.append(d)
            return st

        start = state(frozenset(a.I))
        while work:
            d = work.pop()
            src = index[d]
            outs = [t for q in d for t in self.trans[q]]
            atoms = 0
            for ops, _ in outs:
                for op in ops:
                    if op[0] != 0 or op[3]:
                        raise SvaUnsupportedError("first_match over local variables is not supported yet")
                    atoms |= op[1] | op[2]
            bits = [1 << i for i in range(atoms.bit_length()) if atoms >> i & 1]
            if len(bits) > max_atoms:
                raise SvaUnsupportedError("first_match over too many distinct conditions")
            for combo in product((0, 1), repeat=len(bits)):
                val = sum(b for b, on in zip(bits, combo) if on)
                nxt = set()
                for ops, dest in outs:
                    g = ops[0]
                    if (val & g[1]) == g[1] and not (val & g[2]):
                        nxt.add(dest)
                if not nxt:
                    continue
                pos = val
                neg = atoms & ~val
                g = ((0, pos, neg, ()),)
                if nxt & a.F:
                    self.add(src, g, final)
                else:
                    self.add(src, g, state(frozenset(nxt)))
        return Frag([start], {final}, False)

    # -- finishing ----------------------------------------------------------
    def finish(self, f: Frag) -> "Nfa":
        """Trim to states reachable from I and co-reachable to F; renumber."""
        fwd = set(f.I)
        stack = list(f.I)
        while stack:
            q = stack.pop()
            for _, d in self.trans[q]:
                if d not in fwd:
                    fwd.add(d)
                    stack.append(d)
        rev: dict = {}
        for q in fwd:
            for _, d in self.trans[q]:
                rev.setdefault(d, []).append(q)
        live = set(q for q in f.F if q in fwd)
        stack = list(live)
        while stack:
            q = stack.pop()
            for p in rev.get(q, ()):
                if p not in live:
                    live.add(p)
                    stack.append(p)
        keep = sorted(live)
        renum = {q: i for i, q in enumerate(keep)}
        trans = []
        for q in keep:
            trans.append([(ops, renum[d]) for ops, d in self.trans[q] if d in renum])
        init = frozenset(renum[q] for q in f.I if q in renum)
        final = frozenset(renum[q] for q in f.F if q in renum)
        return Nfa(trans, init, final, f.nullable)


class Nfa:
    """Runtime form of a trimmed tick automaton."""

    def __init__(self, trans, init, final, nullable):
        self.trans = trans
        self.init = init
        self.final = final
        self.nullable = nullable
        self.has_env = any(op[0] == 1 or (op[0] == 0 and op[3])
                           for ts in trans for ops, _ in ts for op in ops)
        # states with outgoing transitions (threads in other states are finished)
        self.live = frozenset(q for q, ts in enumerate(trans) if ts)
        self.need = [0] * len(trans)
        for q, ts in enumerate(trans):
            m = 0
            for ops, _ in ts:
                for op in ops:
                    if op[0] == 0:
                        m |= op[1] | op[2]
            self.need[q] = m
        if not self.has_env:
            self.simple = [[(ops[0][1], ops[0][2], d) for ops, d in ts] for ts in trans]
        self._step_cache: dict = {}
        self._need_cache: dict = {}

    def needs(self, states) -> int:
        """Base atoms read by the next step from these states (env-free states or threads)."""
        m = self._need_cache.get(states)
        if m is None:
            m = 0
            need = self.need
            for q in states:
                m |= need[q if type(q) is int else q[0]]
            self._need_cache[states] = m
        return m

    def step(self, states: frozenset, bits: int):
        """Env-free step. Returns (next live states, matched)."""
        key = (states, bits & self.needs(states))
        hit = self._step_cache.get(key)
        if hit is not None:
            return hit
        nxt = set()
        simple = self.simple
        for q in states:
            for pos, neg, d in simple[q]:
                if (bits & pos) == pos and not (bits & neg):
                    nxt.add(d)
        matched = not self.final.isdisjoint(nxt)
        hit = (frozenset(nxt & self.live), matched)
        if len(self._step_cache) > 200_000:
            self._step_cache.clear()
        self._step_cache[key] = hit
        return hit

    def step_env(self, threads: frozenset, val):
        """Step threads (state, env). Returns (next live threads, [env of each match])."""
        nxt = set()
        matches = []
        final = self.final
        live = self.live
        bits = val.bits
        for q, env in threads:
            for ops, d in self.trans[q]:
                e = env
                ok = True
                for op in ops:
                    if op[0] == 0:
                        if (bits & op[1]) != op[1] or (bits & op[2]):
                            ok = False
                            break
                        for idx, pol in op[3]:
                            if bool(val.env_atom(idx, e)) != pol:
                                ok = False
                                break
                        if not ok:
                            break
                    else:
                        lst = list(e)
                        for local, act in op[1]:
                            lst[local] = val.action(act, e)
                        e = tuple(lst)
                if not ok:
                    continue
                if d in final:
                    matches.append(e)
                if d in live:
                    nxt.add((d, e))
        return frozenset(nxt), matches
