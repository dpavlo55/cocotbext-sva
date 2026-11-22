"""Run compiled assertions over an explicit trace (no simulator).

Used by the engine tests, the fuzzer and the golden-corpus replay, and as the
basis of offline checking. A trace is a list of rows (one per clock tick), each
a dict name -> int, optionally with X masks under the key (name, "x").
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ._compile import Compiler, NamedDef, gen_value_fn
from ._engine import GatedStep, Monitor, build_runner, make_kernel
from ._expr import ConstRef, SignalRef
from ._parser import parse_assertion
from ._errors import SvaError
from ._prop import FAIL, PASS


class TraceBinder:
    def __init__(self, signals: dict, params: dict | None = None, resolver=None, names=None):
        # signals: name -> width or (width, signed); resolver(name) -> width or
        # (width, signed) or None resolves other names on demand (offline
        # checking against a waveform dump); names() lists them for hints
        self.resolver = resolver
        self.names = names
        self.signals = {}
        for name, spec in signals.items():
            w, s = (spec, False) if isinstance(spec, int) else spec
            self.signals[name] = (w, s)
        self.params = dict(params or {})
        self.slot_names: list[str] = []
        self.slot_index: dict[str, int] = {}
        self.xslot_names: list[str] = []
        self.channels: list = []          # (key, value_fn, gate_fn, depth)
        self.channel_index: dict = {}

    def lookup(self, path):
        name = ".".join(path)
        if name in self.params:
            v = self.params[name]
            return ConstRef(v) if isinstance(v, int) else v
        if name not in self.signals:
            spec = self.resolver(name) if self.resolver is not None else None
            if spec is None:
                return None
            self.signals[name] = (spec, False) if isinstance(spec, int) else spec
        w, s = self.signals[name]
        slot = self.slot_index.get(name)
        if slot is None:
            slot = self.slot_index[name] = len(self.slot_names)
            self.slot_names.append(name)
        return SignalRef(slot, w, s, label=name)

    def suggest(self, path):
        import difflib
        known = list(self.signals) + (list(self.names()) if self.names is not None else [])
        return difflib.get_close_matches(".".join(path), known, n=3)

    def x_slot(self, ref):
        name = self.slot_names[ref.slot]
        if name not in self.xslot_names:
            self.xslot_names.append(name)
        return self.xslot_names.index(name)

    def history_channel(self, key, code, width, gcode, depth):
        k = self.channel_index.get(key)
        if k is None:
            k = self.channel_index[key] = len(self.channels)
            vfn = gen_value_fn(code, width)
            gfn = gen_value_fn(gcode) if gcode is not None else None
            self.channels.append((key, vfn, gfn, depth))
        return k

    def endpoint_channel(self, key, fn):
        """Channel whose value is computed at each tick (depth 0): the end
        points of a sequence, for `.triggered`."""
        k = self.channel_index.get(key)
        if k is None:
            k = self.channel_index[key] = len(self.channels)
            self.channels.append((key, fn, None, 0))
        return k


@dataclass
class Event:
    kind: str            # 'fail' | 'pass' | 'end_fail'
    assertion: str
    tick: int            # tick of the verdict
    starts: tuple        # start ticks of the attempts (oldest first, capped)
    count: int


@dataclass
class TraceResult:
    events: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def fails(self, name=None):
        return [(e.tick, e.starts, e.count) for e in self.events
                if e.kind in ("fail", "end_fail") and (name is None or e.assertion == name)]


class _Sink:
    wants_pass = True
    wants_vacuous = True

    def __init__(self, result):
        self.result = result

    def __call__(self, verdict, mon, count, starts, tickno, at_end=False):
        kind = "end_fail" if at_end else {FAIL: "fail", PASS: "pass"}.get(verdict, "vpass")
        self.result.events.append(Event(kind, mon.name, tickno,
                                        tuple(sorted(s[0] for s in starts)), count))


class _Domain:
    def __init__(self):
        self.tickno = 0
        self.ev = -1

    def now(self):
        return self.tickno


class TraceRun:
    """Streaming evaluation of assertions over rows fed one tick at a time
    (step), ended by finish(). run_trace and the offline checker use it.

    `initial` primes the $past histories: a row, None (no default value), or
    "first" (the first row stepped)."""

    def __init__(self, assertions: dict, signals: dict, params: dict | None = None,
                 named: dict | None = None, kinds: dict | None = None, initial=None, once=(),
                 sink=None, resolver=None, names=None, clock: str | None = None,
                 force_multiclock: bool = False, specs: dict | None = None, clock_id=None):
        # specs: name -> parsed AssertionSpec (else parsed from the text);
        # clock_id(ClockEvent) -> name of the 1-bit row signal telling whether
        # that clock ticks (default: the clock's own signal name)
        binder = self.binder = TraceBinder(signals, params, resolver, names)
        named_defs = {}
        for n, d in (named or {}).items():
            named_defs[n] = d if isinstance(d, NamedDef) else NamedDef(n, _parse_seq(d), (), d)
        self.result = TraceResult()
        self.result.warnings = []
        sink = sink if sink is not None else _Sink(self.result)
        self.dom = _Domain()
        self.mons = []
        steps = {}
        for name, text in assertions.items():
            spec = (specs or {}).get(name) or parse_assertion(text)
            kind = (kinds or {}).get(name, "assert")
            spec, lead = _multiclock(spec, text, binder, named_defs, kind, clock, force_multiclock,
                                     clock_id)
            prog = Compiler(binder, text, named_defs).compile(spec, kind)
            self.result.warnings += prog.warnings
            m = Monitor(prog, name, sink, self.dom)
            self.mons.append(m)
            if lead is not None:
                steps[name] = GatedStep(m, binder.lookup((lead,)).slot, name in once)
        self.nslots = len(binder.slot_names)
        self.nx = len(binder.xslot_names)
        row = self.row = {}
        self.R = [(lambda n=n: row.get(n, 0)) for n in binder.slot_names]
        self.RX = [(lambda n=n: row.get((n, "x"), 0)) for n in binder.xslot_names]
        self.H = [None] * len(binder.channels)
        self.hist = None
        self.initial = initial
        if initial != "first":
            self._prime(initial)
        self.run = build_runner([(m, steps[m.name] if m.name in steps
                                  else m.tick_once if m.name in once
                                  else m.tick_env if m.prog.envmode else m.tick)
                                 for m in self.mons], make_kernel(self.dom))
        self.t = 0

    @property
    def signal_names(self):
        """Signals the assertions read (values, and X masks)."""
        return list(self.binder.slot_names), list(self.binder.xslot_names)

    def _prime(self, initial):
        from collections import deque
        self.hist = []
        row = self.row
        for key, vfn, gfn, depth in self.binder.channels:
            init = None
            if depth == 0:
                self.hist.append(None)
                continue
            if initial is not None:
                row.clear()
                row.update(initial)
                init = vfn([None] * self.nslots, self.R, [None] * self.nx, self.RX,
                           [None] * len(self.binder.channels))
            self.hist.append(deque([init] * depth, maxlen=depth))

    def step(self, r: dict):
        if self.hist is None:
            self._prime(r)
        t = self.t
        dom, row, R, RX, H, hist = self.dom, self.row, self.R, self.RX, self.H, self.hist
        dom.tickno = t
        dom.ev = t
        row.clear()
        row.update(r)
        c = [None] * self.nslots
        cx = [None] * self.nx
        for k, (key, vfn, gfn, depth) in enumerate(self.binder.channels):
            if depth == 0:
                H[k] = vfn(c, R, cx, RX, H)
                continue
            H[k] = hist[k][0]
            cur = vfn(c, R, cx, RX, H)
            if gfn is None or gfn(c, R, cx, RX, H):
                hist[k].append(cur)
        self.run(c, R, cx, RX, H, t)
        self.t = t + 1

    def finish(self, end: bool = True) -> TraceResult:
        if end:
            for m in self.mons:
                m.finish(self.t)
        for m in self.mons:
            self.result.stats[m.name] = m.stats.as_dict()
        return self.result


def run_trace(assertions: dict, signals: dict, rows: list, params: dict | None = None,
              named: dict | None = None, kinds: dict | None = None, initial: dict | None = None,
              finish: bool = True, once=(), clock: str | None = None,
              force_multiclock: bool = False) -> TraceResult:
    """Evaluate assertions {name: text} on rows. Ticks are numbered from 0.

    Assertions named in `once` start a single attempt at the first tick (SV:
    a concurrent assertion in an initial block).

    Multi-clock assertions (`@(posedge c1) a ##1 @(posedge c2) b`): a row is a
    time step in which at least one clock ticks, and the 1-bit signal named
    like a clock tells whether that clock ticks in it (edges and `iff` of the
    clocking events are not looked at). `clock` names the default clock of
    assertions that do not start with a clocking event. With
    force_multiclock every assertion is evaluated that way, single-clock
    ones too (rows where their clock does not tick are skipped by the
    rewritten property instead of being left out of the trace)."""
    tr = TraceRun(assertions, signals, params, named, kinds, initial, once, clock=clock,
                  force_multiclock=force_multiclock)
    for r in rows:
        tr.step(r)
    return tr.finish(finish)


def _multiclock(spec, text, binder, named, kind, clock, force, clock_id=None):
    """(spec to compile, name of the leading clock's tick signal or None for
    a single-clock assertion evaluated on every row)."""
    from . import _ast as A
    from ._expr import ExprCompiler
    from ._multiclock import Rewriter, uses_clocks

    if clock_id is None:
        def clock_id(ev):
            if not isinstance(ev.signal, A.Id):
                raise SvaError("a clock must be a signal name")
            return ev.signal.name

    default = clock_id(spec.clock) if spec.clock is not None else clock
    if not force and not uses_clocks(spec.prop, named):
        return spec, None
    if default is None:
        default = "<none>"

    def tick(c):
        if c == "<none>":
            raise SvaError("no clock: part of the property has no clocking event")
        binder.signals.setdefault(c, (1, False))
        return A.Id((c,))

    rw = Rewriter(clock_id, tick, ExprCompiler(binder, text).const, named, kind, text)
    # (also when every clocking event names the same clock: the rows are the
    # time steps of all the clocks of the trace, and this one may not tick in all)
    prop, lead = rw.rewrite(spec.prop, default)
    named.update(rw.extra_named)
    return A.AssertionSpec(spec.clock, spec.disable, prop, span=spec.span), lead


def _parse_seq(text):
    from ._parser import parse_sequence_or_property
    return parse_sequence_or_property(text)
