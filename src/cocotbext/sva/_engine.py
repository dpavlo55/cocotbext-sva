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

from ._prop import FAIL, PASS, VFAIL, VPASS, Val

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
        self.n_attempts = self.n_passes = self.n_vacuous = self.n_failures = 0
        self.n_disabled = self.n_pending_fail = 0
        self.enabled = True           # $asserton/$assertoff: start new attempts?
        # Idle tracking (for idle sleep): after a tick with nothing in flight
        # before and after it, idle_v is that tick's outcome (None: no attempt
        # started, else -verdict of the attempt that resolved at once);
        # 1 means not idle.
        self.idle_v = 1
        self._start_bit = prog.start_bit
        self._sink_vac = getattr(sink, "wants_vacuous", False)
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
            tr = (self._gstate(()), moves, new) + self._plan(list(moves), new, 0)
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
            tr = (self._gstate(tuple(order)), tuple(moves), new) + self._plan(moves, new, len(order))
        self._ntrans += 1
        if self._ntrans > _CACHE_LIMIT:
            self._flush()
        gs.trans[bits] = tr
        return tr

    @staticmethod
    def _plan(moves, new, ndest):
        """(perm, verdicts) for the fast metadata update, or (None, None) if
        attempts merge. perm[j] = source index of destination j (-1: the new
        attempt); verdicts = ((source index or -1, verdict), ...)."""
        dests = [mv for mv in moves if mv >= 0] + ([new] if new is not None and new >= 0 else [])
        if len(dests) != len(set(dests)):
            return (None, None)
        perm = [0] * ndest
        for i, mv in enumerate(moves):
            if mv >= 0:
                perm[mv] = i
        if new is not None and new >= 0:
            perm[new] = -1
        verdicts = tuple((i, -mv) for i, mv in enumerate(moves) if mv < 0)
        if new is not None and new < 0:
            verdicts += ((-1, -new),)
        return (tuple(perm), verdicts)

    def _flush(self):
        for g in self._gstates.values():
            g.trans.clear()
        self._ntrans = 0

    def tick(self, c, R, cx, RX, H, tickno):
        """Evaluate one clock tick (fast path)."""
        gs = self.gs
        if self.enabled:
            bits = gs.bitsfn(c, R, cx, RX, H) | self._start_bit
            self.n_attempts += 1
        else:
            bits = gs.bitsfn(c, R, cx, RX, H)
        tr = gs.trans.get(bits)
        if tr is None:
            tr = self._expand(gs, bits)
        nxt, moves, new, perm, verdicts = tr
        self.gs = nxt
        if not moves:
            # nothing in flight before this tick (the common, idle case)
            if new == -2:                      # new attempt vacuous at once
                self.idle_v = -2
                self.n_vacuous += 1
                if self._sink_vac:
                    self.sink(VPASS, self, 1, ((tickno, self.domain.now()),), tickno)
                return
            if new is None:
                self.idle_v = None
                return
            if new < 0:
                self.idle_v = new
                self._verdict(-new, None, tickno)
                return
            self.idle_v = 1
            self.meta = [(1, ((tickno, self.domain.now()),))]
            return
        self.idle_v = 1
        old = self.meta
        if perm is not None:
            for i, v in verdicts:
                if v == 2 and i >= 0 and not self._sink_vac:
                    self.n_vacuous += old[i][0]
                else:
                    self._verdict(v, old[i] if i >= 0 else None, tickno)
            if new is not None and new >= 0:
                nm = (1, ((tickno, self.domain.now()),))
                self.meta = [old[i] if i >= 0 else nm for i in perm]
            else:
                self.meta = [old[i] for i in perm]
            return
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

    @property
    def stats(self) -> Stats:
        st = Stats()
        st.attempts, st.passes, st.vacuous = self.n_attempts, self.n_passes, self.n_vacuous
        st.failures, st.disabled, st.pending_fail = self.n_failures, self.n_disabled, self.n_pending_fail
        return st

    def _verdict(self, v, meta, tickno):
        count = meta[0] if meta else 1
        if v == VPASS:
            self.n_vacuous += count
            if self.sink.wants_vacuous:
                self.sink(VPASS, self, count, self._starts(meta, tickno), tickno)
            return
        if v == PASS:
            self.n_passes += count
            if self.prog.kind == "cover" or self.sink.wants_pass:
                self.sink(PASS, self, count, self._starts(meta, tickno), tickno)
            return
        if v == FAIL or v == VFAIL:
            if self.prog.kind == "cover":
                return            # a cover attempt that does not match is not an error
            self.n_failures += count
            self.sink(FAIL, self, count, self._starts(meta, tickno), tickno)
            return
        self.n_disabled += count

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
            self.idle_v = None
            return
        if start:
            self.n_attempts += 1
        bits = self._allbits(c, R, cx, RX, H)
        if bits & prog.disable_bit:
            for t, m in toks.items():
                self._verdict(DISABLED, m, tickno)
            self.tokens = {}
            self.idle_v = (-DISABLED if start else None) if not toks else 1
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
        iv = None
        for t, m in items:
            v, t2 = step(t, val)
            if v is None:
                if m is None:
                    m = (1, ((tickno, self.domain.now()),))
                nxt[t2] = _merge(nxt.get(t2), m)
            else:
                if m is None:
                    iv = -v
                self._verdict(v, m, tickno)
        self.tokens = nxt
        self.idle_v = iv if not toks and not nxt else 1

    # -- single attempt (SV: concurrent assertion in an initial block) --------
    def tick_once(self, c, R, cx, RX, H, tickno):
        """Like tick/tick_env, but only the first tick starts an attempt."""
        if self.prog.envmode:
            self.tick_env(c, R, cx, RX, H, tickno)
        else:
            self.tick(c, R, cx, RX, H, tickno)
        self.enabled = False
        if self.idle_v != 1:
            self.idle_v = None        # later ticks start nothing

    # -- idle sleep ---------------------------------------------------------
    def sleepable(self) -> bool:
        """Idle, and k more identical ticks can be accounted for without
        evaluating them (no report or callback is due on such a tick)."""
        v = self.idle_v
        if v is None:
            return True
        if v == -VPASS:
            return not self._sink_vac
        if v == -PASS:
            return not (self.prog.kind == "cover" or self.sink.wants_pass)
        if v == -FAIL or v == -VFAIL:
            return self.prog.kind == "cover"   # a non-matching cover attempt is silent
        return v == -DISABLED

    def idle_slots(self):
        """(slots, xslots) the idle state reads each tick."""
        if self.prog.envmode:
            return self.prog.all_slots()
        return self.prog.slots_of(self.gs.need)

    def skip(self, k: int):
        """Account for k idle ticks identical to the last one."""
        if self.enabled:
            self.n_attempts += k
        v = self.idle_v
        if v == -VPASS:
            self.n_vacuous += k
        elif v == -PASS:
            self.n_passes += k
        elif v == -DISABLED:
            self.n_disabled += k

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
                self.n_failures += meta[0]
                self.n_pending_fail += meta[0]
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
