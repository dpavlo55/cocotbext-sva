"""Runtime evaluation of compiled assertions.

Fast path (no local variables): all in-flight attempts of an assertion form one
*global state* (a canonical tuple of attempt terms). Transitions are computed
lazily on first use for a (global state, input bits) pair and cached, so a
steady-state tick costs one generated bits function + one dict lookup,
independent of the number of attempts in flight. Per-attempt metadata (count
of merged attempts, start ticks/times) is carried alongside only while attempts
are in flight.

Token path (local variables): attempt terms carry their local values, so they
are stepped individually every tick (identical terms are still merged).
"""
from __future__ import annotations

from ._prop import FAIL, PASS, VPASS, Val

DISABLED = 4
MAX_STARTS = 16          # start times remembered per merged term
_CACHE_LIMIT = 100_000   # transitions cached per assertion before flushing


class GState:
    __slots__ = ("terms", "need", "bitsfn", "trans")

    def __init__(self, terms, need, bitsfn):
        self.terms = terms
        self.need = need
        self.bitsfn = bitsfn
        self.trans = {}


def _merge(m1, m2):
    if m1 is None:
        return m2
    count = m1[0] + m2[0]
    starts = m1[1] + m2[1]
    if len(starts) > MAX_STARTS:
        starts = tuple(sorted(starts)[:MAX_STARTS])
    return (count, starts)


class Stats:
    __slots__ = ("attempts", "passes", "vacuous", "failures", "disabled", "pending_fail")

    def __init__(self):
        self.attempts = self.passes = self.vacuous = self.failures = self.disabled = 0
        self.pending_fail = 0

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Monitor:
    """One assertion instance on one clocking domain."""

    def __init__(self, prog, name: str, sink, domain):
        self.prog = prog
        self.name = name
        self.sink = sink              # sink(event, monitor, count, starts, tick)
        self.domain = domain
        self.stats = Stats()
        self.enabled = True           # $asserton/$assertoff: start new attempts?
        self.tmpl = prog.tmpl
        self._init_term = prog.tmpl.init(tuple([None] * prog.nlocals)) if prog.envmode else prog.tmpl.init(())
        self._intern: dict = {}
        self._gstates: dict = {}
        self._ntrans = 0
        if prog.envmode:
            self.tokens: dict = {}    # term -> meta
            envfns, actfns = prog.env_fns()
            self._envfns, self._actfns = envfns, actfns
            allmask = (1 << len(prog.atoms)) - 1
            self._allbits = prog.bits_fn(allmask)
        else:
            self.gs = self._gstate(())
            self.meta = []

    # -- fast path ----------------------------------------------------------
    def _term_id(self, t):
        i = self._intern.get(t)
        if i is None:
            i = self._intern[t] = len(self._intern)
        return i

    def _gstate(self, terms):
        gs = self._gstates.get(terms)
        if gs is None:
            tmpl = self.tmpl
            need = tmpl.init_need | self.prog.disable_bit
            for t in terms:
                need |= tmpl.needs(t)
            gs = GState(terms, need, self.prog.bits_fn(need))
            self._gstates[terms] = gs
        return gs

    def _expand(self, gs, bits):
        prog = self.prog
        start = bits & prog.start_bit
        if bits & prog.disable_bit:
            moves = (-DISABLED,) * len(gs.terms)
            new = -DISABLED if start else None
            tr = (self._gstate(()), moves, new)
        else:
            val = Val(bits)
            step = self.tmpl.step
            results = []
            for t in gs.terms:
                results.append(step(t, val))
            if start:
                results.append(step(self._init_term, val))
            live = {}
            for v, t2 in results:
                if v is None:
                    live[t2] = self._term_id(t2)
            order = sorted(live, key=live.get)
            pos = {t: i for i, t in enumerate(order)}
            moves = []
            for v, t2 in results:
                moves.append(-v if v is not None else pos[t2])
            new = moves.pop() if start else None
            tr = (self._gstate(tuple(order)), tuple(moves), new)
        self._ntrans += 1
        if self._ntrans > _CACHE_LIMIT:
            self._flush()
        gs.trans[bits] = tr
        return tr

    def _flush(self):
        for g in self._gstates.values():
            g.trans.clear()
        self._ntrans = 0

    def tick(self, c, R, cx, RX, H, tickno):
        """Evaluate one clock tick (fast path)."""
        gs = self.gs
        bits = gs.bitsfn(c, R, cx, RX, H)
        if self.enabled:
            bits |= self.prog.start_bit
            self.stats.attempts += 1
        tr = gs.trans.get(bits)
        if tr is None:
            tr = self._expand(gs, bits)
        nxt, moves, new = tr
        self.gs = nxt
        if not moves:
            if new is None:
                return
            if new < 0:
                self._verdict(-new, None, tickno)
                return
            self.meta = [(1, ((tickno, self.domain.now()),))]
            return
        old = self.meta
        meta = [None] * len(nxt.terms)
        for i, mv in enumerate(moves):
            if mv >= 0:
                meta[mv] = _merge(meta[mv], old[i])
            else:
                self._verdict(-mv, old[i], tickno)
        if new is not None:
            if new >= 0:
                meta[new] = _merge(meta[new], (1, ((tickno, self.domain.now()),)))
            else:
                self._verdict(-new, None, tickno)
        self.meta = meta

    def _verdict(self, v, meta, tickno):
        st = self.stats
        count = meta[0] if meta else 1
        if v == VPASS:
            st.vacuous += count
            if self.sink.wants_vacuous:
                self.sink(VPASS, self, count, self._starts(meta, tickno), tickno)
            return
        if v == PASS:
            st.passes += count
            if self.prog.kind == "cover" or self.sink.wants_pass:
                self.sink(PASS, self, count, self._starts(meta, tickno), tickno)
            return
        if v == FAIL:
            if self.prog.kind == "cover":
                return            # a cover attempt that does not match is not an error
            st.failures += count
            self.sink(FAIL, self, count, self._starts(meta, tickno), tickno)
            return
        st.disabled += count

    def _starts(self, meta, tickno):
        if meta is None:
            return ((tickno, self.domain.now()),)
        return meta[1]

    # -- token path ---------------------------------------------------------
    def tick_env(self, c, R, cx, RX, H, tickno):
        prog = self.prog
        toks = self.tokens
        start = self.enabled
        if not toks and not start:
            return
        if start:
            self.stats.attempts += 1
        bits = self._allbits(c, R, cx, RX, H)
        if bits & prog.disable_bit:
            for t, m in toks.items():
                self._verdict(DISABLED, m, tickno)
            self.tokens = {}
            if start:
                self._verdict(DISABLED, None, tickno)
            return
        ctx = _EnvCtx(self._envfns, self._actfns, c, R, cx, RX, H)
        val = Val(bits, ctx)
        step = self.tmpl.step
        nxt = {}
        items = list(toks.items())
        if start:
            items.append((self._init_term, None))
        for t, m in items:
            v, t2 = step(t, val)
            if v is None:
                if m is None:
                    m = (1, ((tickno, self.domain.now()),))
                nxt[t2] = _merge(nxt.get(t2), m)
            else:
                self._verdict(v, m, tickno)
        self.tokens = nxt

    # -- end of test --------------------------------------------------------
    def pending(self):
        """[(term, meta)] of attempts still in flight."""
        if self.prog.envmode:
            return list(self.tokens.items())
        return list(zip(self.gs.terms, self.meta))

    def finish(self, tickno):
        """End of evaluation: strong obligations still pending fail."""
        for term, meta in self.pending():
            if self.prog.kind != "cover" and not self.tmpl.finish(term):
                self.stats.failures += meta[0]
                self.stats.pending_fail += meta[0]
                self.sink(FAIL, self, meta[0], meta[1], tickno, at_end=True)
        if self.prog.envmode:
            self.tokens = {}
        else:
            self.gs = self._gstate(())
            self.meta = []

    def kill(self):
        """$assertkill: drop attempts in flight without verdicts."""
        if self.prog.envmode:
            self.tokens = {}
        else:
            self.gs = self._gstate(())
            self.meta = []


class _EnvCtx:
    __slots__ = ("envfns", "actfns", "args")

    def __init__(self, envfns, actfns, c, R, cx, RX, H):
        self.envfns = envfns
        self.actfns = actfns
        self.args = (c, R, cx, RX, H)

    def env_atom(self, idx, env):
        return self.envfns[idx](*self.args, env)

    def action(self, idx, env):
        return self.actfns[idx](*self.args, env)
