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

import os
from operator import itemgetter

from ._prop import FAIL, PASS, VFAIL, VPASS, Val
from ._symenv import SymCache

# Optional compiled kernel (separate distribution cocotbext-sva-kernel).
# COCOTBEXT_SVA_KERNEL: auto (default: use it if installed), python, native.
KERNEL_MODE = os.environ.get("COCOTBEXT_SVA_KERNEL", "auto").lower()
_kmod = None
if KERNEL_MODE != "python":
    try:
        import cocotbext_sva_kernel as _kmod
    except ImportError:
        if KERNEL_MODE == "native":
            raise

_NOQUIET = frozenset()

DISABLED = 4
# local-variable attempts stepped symbolically (False: concrete terms, for testing)
SYMBOLIC = os.environ.get("COCOTBEXT_SVA_SYMBOLIC", "1") != "0"
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

    __slots__ = ("prog", "name", "sink", "domain", "n_passes", "n_vacuous", "n_failures",
                 "n_disabled", "n_pending_fail", "_enabled", "_att", "_en_ev", "_sb", "qs",
                 "_quiet", "last_ev", "idle_v", "_start_bit", "_sink_vac", "tmpl", "_init_term",
                 "_intern", "_gstates", "_ntrans", "tokens", "_envfns", "_actfns", "_allbits",
                 "_fullbits", "_silent", "gs", "meta", "_k", "_kj", "_gs_list", "_gs_ids", "_sym",
                 "_gated", "__weakref__")

    def __init__(self, prog, name: str, sink, domain):
        self.prog = prog
        self.name = name
        self.sink = sink              # sink(event, monitor, count, starts, tick)
        self.domain = domain
        self.n_passes = self.n_vacuous = self.n_failures = 0
        self.n_disabled = self.n_pending_fail = 0
        self._enabled = True          # $asserton/$assertoff: start new attempts?
        # Attempts are not counted per tick: one starts at every evaluated tick
        # while enabled, so the count is _att plus the evaluated ticks (the
        # domain's ev) since enabling (_en_ev).
        self._att = 0
        self._en_ev = domain.ev
        self._gated = False           # attempts are started (and counted) by a GatedStep
        self._sb = prog.start_bit     # start bit while enabled, else 0
        # Quiet ticks: idle ticks whose only effect is on counters (the same
        # outcome as idle_v) are skipped by the domain runner when the inputs
        # are in `qs` (bits of the idle state, start bit included) and applied
        # lazily: evaluated ticks since last_ev that this monitor did not see
        # were all quiet. qs is empty while quiet ticks are impossible.
        self.qs = _NOQUIET
        self._quiet: dict = {}        # (enabled, idle_v) -> set of bits
        self.last_ev = domain.ev
        # Idle tracking (for idle sleep): after a tick with nothing in flight
        # before and after it, idle_v is that tick's outcome (None: no attempt
        # started, else -verdict of the attempt that resolved at once);
        # 1 means not idle.
        self.idle_v = 1
        # compiled kernel: _k is the domain's KernelDomain and _kj this
        # monitor's index in it once the kernel holds its evaluation state
        # (_kj = -1: evaluated in Python for good)
        self._k = None
        self._kj = None
        self._start_bit = prog.start_bit
        self._sink_vac = getattr(sink, "wants_vacuous", False)
        self.tmpl = prog.tmpl
        self._init_term = prog.tmpl.init(tuple([None] * prog.nlocals)) if prog.envmode else prog.tmpl.init(())
        self._intern: dict = {}
        self._gstates: dict = {}
        self._ntrans = 0
        if prog.envmode:
            self.tokens: dict = {}    # term (symbolic: (shape id, envs)) -> meta
            envfns, actfns = prog.env_fns()
            self._envfns, self._actfns = envfns, actfns
            self._sym = SymCache(prog.tmpl, prog.nlocals, envfns, actfns) if SYMBOLIC else None
            allmask = (1 << len(prog.atoms)) - 1
            self._allbits = prog.bits_fn(allmask)
        else:
            self._fullbits = prog.bits_fn((1 << len(prog.atoms)) - 1)
            # verdicts that only increment a counter (no sink call)
            silent = set()
            if not self._sink_vac:
                silent.add(VPASS)
            if not (prog.kind == "cover" or getattr(sink, "wants_pass", False)):
                silent.add(PASS)
            self._silent = frozenset(silent)
            self.gs = self._gstate(())
            self.meta = ()
            self._gs_list = [self.gs]      # global state ids for the kernel
            self._gs_ids = {(): 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, v: bool):
        self.settle()
        v = bool(v)
        if v != self._enabled and not self._gated:
            if v:
                self._en_ev = self.domain.ev
            else:
                self._att += self.domain.ev - self._en_ev
        self._enabled = v
        self._sb = self._start_bit if v else 0
        self.qs = _NOQUIET
        if self._k is not None:
            self._k.K.set_enabled(self._kj, v)

    @property
    def n_attempts(self) -> int:
        if self._gated:
            return self._att
        return self._att + (self.domain.ev - self._en_ev if self._enabled else 0)

    def settle(self):
        """Apply the quiet ticks skipped since this monitor last ran."""
        if self.qs:
            ev = self.domain.ev
            k = ev - self.last_ev
            self.last_ev = ev
            if k:
                self.skip(k)

    def _learn_quiet(self, raw_bits):
        """After an idle tick with nothing in flight before and after it."""
        key = (self._enabled, self.idle_v)
        q = self._quiet.get(key)
        if q is None:
            q = self._quiet[key] = set() if self.sleepable() else False
        if q is not False:
            q.add(raw_bits | self._start_bit)
            self.qs = q
            self.last_ev = self.domain.ev

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
        nxt, moves, new = self._successor(gs, bits)
        tr = self._split((nxt, moves, new) + self._plan(list(moves), new, len(nxt.terms)))
        self._ntrans += 1
        if self._ntrans > _CACHE_LIMIT:
            self._flush()
        gs.trans[bits] = tr
        return tr

    def _kexpand(self, sid, bits):
        """Transition for the compiled kernel: (next state id, its need, its
        number of attempts, moves, new, perm or None)."""
        nxt, moves, new = self._successor(self._gs_list[sid], bits)
        perm = self._plan(list(moves), new, len(nxt.terms))[0]
        nid = self._gs_ids.get(nxt.terms)
        if nid is None:
            nid = self._gs_ids[nxt.terms] = len(self._gs_list)
            self._gs_list.append(nxt)
        return (nid, nxt.need, len(nxt.terms), list(moves), new, None if perm is None else list(perm))

    def _successor(self, gs, bits):
        """(next global state, moves, new) for the inputs bits."""
        prog = self.prog
        start = bits & prog.start_bit
        if bits & prog.disable_bit:
            moves = (-DISABLED,) * len(gs.terms)
            new = -DISABLED if start else None
            return self._gstate(()), moves, new
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
            return self._gstate(tuple(order)), tuple(moves), new

    def _split(self, tr):
        """(next, moves, new, perm, loud, spass, svac, nvac): the verdicts of
        a non-merging transition split into reported ones (loud: (source or
        -1, verdict)), counter-only passes and vacuous passes of attempts in
        flight (source indices), and nvac = 1 if the new attempt is a
        counter-only vacuous pass."""
        nxt, moves, new, perm, verdicts = tr
        if perm is None:
            return (nxt, moves, new, None, None, 0, None)
        loud, spass, svac, nvac = [], [], [], 0
        for i, v in verdicts:
            if v in self._silent and (i >= 0 or v == VPASS):
                if i < 0:
                    nvac = 1
                elif v == PASS:
                    spass.append(i)
                else:
                    svac.append(i)
            else:
                loud.append((i, v))
        # metadata of the next state from the old one (plus the new attempt's
        # at index -1)
        if len(perm) > 1:
            pg = itemgetter(*perm)
        elif perm:
            pg = (lambda m, _p=perm[0]: (m[_p],))
        else:
            pg = (lambda m: ())
        lists = (tuple(loud), tuple(spass), tuple(svac)) if loud or spass or svac else None
        start = new is not None and new >= 0
        return (nxt, moves, new, perm, pg, nvac, lists) if not start else \
            (nxt, moves, new, perm, pg, nvac + 2, lists)

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
        self.tickb(self._fullbits(c, R, cx, RX, H), tickno)

    def tickb(self, full, tickno):
        """tick() given the bits of all base atoms (computed by the domain
        runner, shared between assertions)."""
        gs = self.gs
        if self.qs:
            k = self.domain.ev - self.last_ev - 1
            if k:
                self.skip(k)
            self.qs = _NOQUIET
        bits = (full & gs.need) | self._sb
        tr = gs.trans.get(bits)
        if tr is None:
            tr = self._expand(gs, bits)
        nxt, moves, new, perm, pg, nvac, lists = tr
        self.gs = nxt
        if perm is not None and moves:
            # attempts in flight, no merging (the common busy case). nvac:
            # bit 0 = the new attempt is a counter-only vacuous pass, bit 1 =
            # the new attempt stays in flight
            if nvac & 1:
                self.n_vacuous += 1
            old = self.meta
            if lists is not None:
                loud, spass, svac = lists
                for i in spass:
                    self.n_passes += old[i][0]
                for i in svac:
                    self.n_vacuous += old[i][0]
                for i, v in loud:
                    self._verdict(v, old[i] if i >= 0 else None, tickno)
            if nvac & 2:
                self.meta = pg(old + ((1, ((tickno, self.domain.now()),)),))
            else:
                self.meta = pg(old)
                if nvac & 1 and not nxt.terms:
                    # now idle, and this tick's new attempt was a quiet vacuous
                    # pass: what an idle tick with these inputs does
                    self.idle_v = -VPASS
                    self._learn_quiet(full & nxt.need)
            return
        raw = bits & ~self._sb
        if not moves:
            # nothing in flight before this tick (the common, idle case)
            if new == -2:                      # new attempt vacuous at once
                self.idle_v = -2
                self.n_vacuous += 1
                if self._sink_vac:
                    self.sink(VPASS, self, 1, ((tickno, self.domain.now()),), tickno)
                else:
                    self._learn_quiet(raw)
                return
            if new is None:
                self.idle_v = None
                self._learn_quiet(raw)
                return
            if new < 0:
                self.idle_v = new
                self._verdict(-new, None, tickno)
                self._learn_quiet(raw)
                return
            self.idle_v = 1
            self.meta = ((1, ((tickno, self.domain.now()),)),)
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
        self.meta = tuple(meta)

    @property
    def stats(self) -> Stats:
        self.settle()
        st = Stats()
        st.attempts, st.passes, st.vacuous = self.n_attempts, self.n_passes, self.n_vacuous
        st.failures, st.disabled, st.pending_fail = self.n_failures, self.n_disabled, self.n_pending_fail
        if self._k is not None:
            p, v, f, d = self._k.K.stats(self._kj)
            st.passes += p
            st.vacuous += v
            st.failures += f
            st.disabled += d
        return st

    def _kemit(self, v, count, ticks, tickno):
        """Report a verdict computed by the kernel (counters already updated)."""
        t_of = self._k.time_of
        self.sink(v, self, count, tuple((t, t_of(t)) for t in ticks), tickno)

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
        if self._sym is not None:
            self.tick_sym(self._allbits(c, R, cx, RX, H), c, R, cx, RX, H, tickno)
            return
        prog = self.prog
        toks = self.tokens
        start = self._sb              # (nonzero while attempts are started)
        if self.qs:
            k = self.domain.ev - self.last_ev - 1
            if k:
                self.skip(k)
            self.qs = _NOQUIET
        if not toks and not start:
            self.idle_v = None
            return
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

    def tick_sym(self, bits, c, R, cx, RX, H, tickno):
        """tick_env on (shape id, envs) attempts (see _symenv), given the
        bits of all base atoms."""
        toks = self.tokens
        start = self._sb
        if self.qs:
            k = self.domain.ev - self.last_ev - 1
            if k:
                self.skip(k)
            self.qs = _NOQUIET
        if not toks and not start:
            self.idle_v = None
            return
        if bits & self.prog.disable_bit:
            for t, m in toks.items():
                self._verdict(DISABLED, m, tickno)
            self.tokens = {}
            self.idle_v = (-DISABLED if start else None) if not toks else 1
            if start:
                self._verdict(DISABLED, None, tickno)
                if not toks:
                    self._learn_quiet(bits & self.idle_need())
            return
        sym = self._sym
        trans, needs = sym.trans, sym.need
        args = (c, R, cx, RX, H)
        nxt = {}
        quiet_vac = not self._sink_vac
        for key, m in toks.items():
            sid, E = key
            node = trans[sid].get(bits & needs[sid])
            if type(node) is tuple:
                v, sid2, mk, ok = node
                if v is None:
                    if mk is None:
                        E2 = E
                    else:
                        E2 = mk(E, *args)
                        if not ok:
                            sid2, E2 = sym.canonical(sid2, E2)
            else:
                v, sid2, E2 = sym.step(sid, E, bits, args)
            if v is None:
                k2 = key if (sid2 == sid and E2 is E) else (sid2, E2)
                old = nxt.get(k2)
                nxt[k2] = m if old is None else _merge(old, m)
            elif v == VPASS and quiet_vac:
                self.n_vacuous += m[0]
            else:
                self._verdict(v, m, tickno)
        iv = None
        if start:
            sid = sym.init_sid
            node = trans[sid].get(bits & needs[sid])
            if node is not None and node[0] == VPASS and quiet_vac:
                v = VPASS                    # (a leaf: query nodes hold a function first)
            else:
                v, sid2, E2 = sym.step(sid, sym.init_envs, bits, args)
            if v is None:
                k2 = (sid2, E2)
                m = (1, ((tickno, self.domain.now()),))
                old = nxt.get(k2)
                nxt[k2] = m if old is None else _merge(old, m)
            elif v == VPASS and quiet_vac:
                self.n_vacuous += 1
                iv = -VPASS
            else:
                iv = -v
                self._verdict(v, None, tickno)
        self.tokens = nxt
        if toks or nxt:
            self.idle_v = 1
            return
        self.idle_v = iv
        # quiet if the outcome depended on base atoms only (no env condition)
        if type(trans[sid].get(bits & needs[sid])) is tuple:
            self._learn_quiet(bits & self.idle_need())

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
        if self._k is not None:
            return self._k.K.sleepable(self._kj)
        v = self.idle_v
        if v is None:
            return True
        if v == 1:
            return False
        if v == -VPASS:
            return not self._sink_vac
        if v == -PASS:
            return not (self.prog.kind == "cover" or self.sink.wants_pass)
        if v == -FAIL or v == -VFAIL:
            return self.prog.kind == "cover"   # a non-matching cover attempt is silent
        return v == -DISABLED

    def idle_need(self) -> int:
        """Base atoms an idle tick reads (symbolic env mode)."""
        return self._sym.need[self._sym.init_sid] | self.prog.disable_bit

    def idle_slots(self):
        """(slots, xslots) the idle state reads each tick."""
        if self.prog.envmode:
            if self._sym is not None and not self.tokens:
                return self.prog.slots_of(self.idle_need())
            return self.prog.all_slots()
        if self._k is not None:
            return self.prog.slots_of(self._gs_list[0].need)
        return self.prog.slots_of(self.gs.need)

    def slept(self, k: int):
        """The domain slept k ticks (already counted in the domain's ev)."""
        if self._k is not None:
            self._k.K.skip(self._kj, k)      # (the domain batches this: KernelDomain.slept)
        elif self.qs:
            self.settle()
        else:
            self.skip(k)

    def skip(self, k: int):
        """Account for k idle ticks identical to the last one (attempts are
        derived from the domain's tick count)."""
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
            if self._sym is not None:
                term = self._sym.term
                return [(term(sid, E), m) for (sid, E), m in self.tokens.items()]
            return list(self.tokens.items())
        if self._k is not None:
            sid, metas = self._k.K.peek(self._kj)
            t_of = self._k.time_of
            return [(term, (count, tuple((t, t_of(t)) for t in ticks)))
                    for term, (count, ticks) in zip(self._gs_list[sid].terms, metas)]
        return list(zip(self.gs.terms, self.meta))

    def finish(self, tickno):
        """End of evaluation: strong obligations still pending fail."""
        self.settle()
        for term, meta in self.pending():
            if self.prog.kind != "cover" and not self.tmpl.finish(term):
                self.n_failures += meta[0]
                self.n_pending_fail += meta[0]
                self.sink(FAIL, self, meta[0], meta[1], tickno, at_end=True)
        if self.prog.envmode:
            self.tokens = {}
        elif self._k is not None:
            self._k.K.reset(self._kj)
        else:
            self.gs = self._gstate(())
            self.meta = ()

    def kill(self):
        """$assertkill: drop attempts in flight without verdicts."""
        self.settle()
        if self.prog.envmode:
            self.tokens = {}
        elif self._k is not None:
            self._k.K.reset(self._kj)
        else:
            self.gs = self._gstate(())
            self.meta = ()


class GatedStep:
    """Step function of a multi-clock assertion. Its domain ticks whenever
    one of the assertion's clocks does; attempts start only at ticks of the
    leading clock (value slot `slot` is nonzero), and those are the attempts
    counted. With once=True only the first such tick starts an attempt."""

    def __init__(self, mon: "Monitor", slot: int, once: bool = False):
        self.mon = mon
        self.slot = slot
        self.once = once
        self.inner = mon.tick_env if mon.prog.envmode else mon.tick
        mon._gated = True
        mon._att = 0

    def __call__(self, c, R, cx, RX, H, tickno):
        m = self.mon
        v = c[self.slot]
        if v is None:
            v = c[self.slot] = R[self.slot]()
        if v and m._enabled:
            m._sb = m._start_bit
            m._att += 1
            self.inner(c, R, cx, RX, H, tickno)
            if self.once:
                m.enabled = False
        else:
            m._sb = 0
            self.inner(c, R, cx, RX, H, tickno)


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


class KernelDomain:
    """The compiled kernel of one clocking domain, and the start times of
    attempts in flight (the kernel keeps tick numbers)."""

    def __init__(self, domain):
        self.K = _kmod.Kernel(MAX_STARTS)
        self.domain = domain
        self.mons = []                 # kernel index -> Monitor
        self.times = {}                # tick -> simulation time
        self.prune_at = 4096

    def register(self, m):
        st = m.sink
        m._kj = self.K.add(m._start_bit if m._enabled else 0, m._gs_list[0].need,
                           m.prog.kind == "cover", bool(getattr(st, "wants_pass", False)),
                           m._sink_vac, m._kexpand)
        if not m._enabled:
            self.K.set_enabled(m._kj, False)
        m._k = self
        self.mons.append(m)

    def slept(self, k):
        """The domain slept k ticks: account for them in every kernel monitor."""
        self.K.skip_all(k)

    def time_of(self, t):
        v = self.times.get(t)
        return self.domain.now() if v is None else v

    def dispatch(self, events):
        mons = self.mons
        for j, v, count, ticks, tick in events:
            mons[j]._kemit(v, count, ticks, tick)

    def prune(self):
        """Forget start times older than every attempt in flight (in place:
        the domain runner holds this dict)."""
        oldest = self.K.min_start()
        times = self.times
        for t in [t for t in times if oldest is None or t < oldest]:
            del times[t]
        if len(times) > 2048:
            self.prune_at = 2 * len(times)    # long-pending attempts: prune less often
        else:
            self.prune_at = 4096


def make_kernel(domain):
    """A KernelDomain for the domain, or None (kernel not installed or disabled)."""
    return KernelDomain(domain) if _kmod is not None else None


def _kernel_eligible(m, step):
    return (getattr(step, "__func__", None) is Monitor.tick and step.__self__ is m
            and len(m.prog.atoms) <= 63)


def build_runner(entries, kd=None):
    """Generate the per-tick function of a clocking domain.

    Returns the number of fast-path monitors it had to call (0: every one of
    them was quiet, so the domain may be able to sleep).

    entries: [(monitor, step)] in evaluation order. A fast-path monitor stepped
    by its own tick() is called only when its idle-state inputs are not in its
    quiet set; the idle-state bits are computed inline, once per distinct
    expression (assertions on the same signals share them)."""
    from ._compile import _compile_fn, _prelude
    from ._expr import Code
    ns, body, exprs, fulls, total, others = {}, [], {}, {}, Code(""), []
    kbody = _kernel_part(entries, kd, ns, total) if kd is not None else []
    for i, (m, step) in enumerate(entries):
        ns[f"S{i}"] = step
        if m._k is not None:
            continue                   # evaluated by the kernel
        if getattr(step, "__func__", None) is Monitor.tick and step.__self__ is m:
            code = m.prog.bits_code(m._gstate(()).need)
            text = f"({code.text}) | {m._start_bit}"
            var = exprs.get(text)
            if var is None:
                var = exprs[text] = f"b{len(exprs)}"
                total.merge(code)
                body.append(f"    {var} = {text}")
            full = m.prog.bits_code((1 << len(m.prog.atoms)) - 1)
            fv = fulls.get(full.text)
            ns[f"M{i}"] = m
            ns[f"T{i}"] = m.tickb
            body.append(f"    if {var} not in M{i}.qs:")
            if fv is None:
                fv = fulls[full.text] = f"f{len(fulls)}"
            body.append(f"        if {fv} is None:")
            body += ["        " + ln for ln in _prelude(full)]
            body.append(f"            {fv} = {full.text}")
            body.append(f"        T{i}({fv}, t)")
            body.append("        n += 1")
        elif getattr(step, "__func__", None) is Monitor.tick_env and step.__self__ is m                 and m._sym is not None:
            code = m.prog.bits_code(m.idle_need())
            text = f"({code.text}) | {m._start_bit}"
            var = exprs.get(text)
            if var is None:
                var = exprs[text] = f"b{len(exprs)}"
                total.merge(code)
                body.append(f"    {var} = {text}")
            full = m.prog.bits_code((1 << len(m.prog.atoms)) - 1)
            fv = fulls.get(full.text)
            ns[f"M{i}"] = m
            ns[f"Y{i}"] = m.tick_sym
            body.append(f"    if {var} not in M{i}.qs:")
            if fv is None:
                fv = fulls[full.text] = f"f{len(fulls)}"
            body.append(f"        if {fv} is None:")
            body += ["        " + ln for ln in _prelude(full)]
            body.append(f"            {fv} = {full.text}")
            body.append(f"        Y{i}({fv}, c, R, cx, RX, H, t)")
            body.append("        n += 1")
        else:
            body.append(f"    S{i}(c, R, cx, RX, H, t)")
            others.append(m)
    init = [f"    {fv} = None" for fv in fulls.values()] + ["    n = 0"]
    src = "\n".join(["def _run(c, R, cx, RX, H, t):"] + _prelude(total) + init + kbody + body
                    + ["    return n"])
    fn = _compile_fn(src, "_run")
    fn.__globals__.update(ns)
    fn.others = others                 # monitors not covered by the returned count
    return fn


def _kernel_part(entries, kd, ns, total):
    """Register eligible monitors with the kernel and generate the code that
    feeds it. Every distinct atom of the kernel's monitors is evaluated once
    and packed into 64-bit words: phase A holds the atoms idle states read
    (always evaluated, passed to tick_a), phase B the others (evaluated only
    for the words tick_a asks for, passed to tick_b)."""
    from ._compile import _prelude
    from ._expr import Code
    mons = []
    for m, step in entries:
        if m._kj is None:
            if not _kernel_eligible(m, step):
                m._kj = -1
                continue
            kd.register(m)
        if m._k is not None:
            mons.append(m)
    active = {m._kj for m in mons}
    for j in range(len(kd.mons)):
        if j not in active:
            kd.K.deactivate(j)
    if not mons:
        return []
    # atom layout
    a_idx, b_idx, a_codes, b_codes = {}, {}, [], []
    for m in mons:
        need0 = m._gs_list[0].need
        for i, code in enumerate(m.prog.atoms):
            if need0 >> i & 1 and code.text not in a_idx:
                a_idx[code.text] = len(a_codes)
                a_codes.append(code)
    for m in mons:
        for code in m.prog.atoms:
            if code.text not in a_idx and code.text not in b_idx:
                b_idx[code.text] = len(b_codes)
                b_codes.append(code)
    na = len(a_codes)
    if (len(b_codes) + 63) // 64 > 60:
        raise NotImplementedError("too many distinct conditions for the compiled kernel")
    kd.K.set_layout(na)
    for m in mons:
        amap, bneed = [], 0
        for code in m.prog.atoms:
            if code.text in a_idx:
                amap.append(a_idx[code.text])
            else:
                g = b_idx[code.text]
                amap.append(na + g)
                bneed |= 1 << (g >> 6)
        kd.K.set_inputs(m._kj, amap, bneed)

    def words(codes):
        out = []
        for w in range(0, len(codes), 64):
            chunk = codes[w:w + 64]
            terms = [f"({c.text})" if k == 0 else f"(({c.text}) << {k})" for k, c in enumerate(chunk)]
            merged = Code("")
            for c in chunk:
                merged.merge(c)
            out.append((" | ".join(terms), merged))
        return out

    lines = []
    awords = words(a_codes) or [("0", Code(""))]
    for w, (text, code) in enumerate(awords):
        total.merge(code)
        lines.append(f"    ka{w} = {text}")
    ns.update(KA=kd.K.tick_a, KB=kd.K.tick_b, KD=kd.dispatch, KT=kd.times, KN=kd.domain.now,
              KP=kd.prune, KDOM=kd)
    atuple = "(" + "".join(f"ka{w}, " for w in range(len(awords))) + ")"
    lines += [f"    r = KA({atuple}, t)",
              "    if r & 4:",
              "        n = 1",
              "    if r > 7 or r & 1:"]
    bwords = words(b_codes)
    if bwords:
        lines += ["        need = r >> 3"]
        for w, (text, code) in enumerate(bwords):
            lines.append(f"        if need & {1 << w}:")
            lines += ["        " + ln for ln in _prelude(code)]
            lines += [f"            kb{w} = {text}",
                      "        else:",
                      f"            kb{w} = 0"]
        btuple = "(" + "".join(f"kb{w}, " for w in range(len(bwords))) + ")"
        lines.append(f"        ev = KB({btuple}, t)")
    else:
        lines.append("        ev = KB((), t)")
    lines += ["        if ev is not None:",
              "            KD(ev)",
              "    if r & 2 or r > 7:",
              "        KT[t] = KN()",
              "        if len(KT) > KDOM.prune_at:",
              "            KP()"]
    return lines
