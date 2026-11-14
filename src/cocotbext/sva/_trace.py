"""Run compiled assertions over an explicit trace (no simulator).

Used by the engine tests, the fuzzer and the golden-corpus replay, and as the
basis of offline checking. A trace is a list of rows (one per clock tick), each
a dict name -> int, optionally with X masks under the key (name, "x").
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ._compile import Compiler, NamedDef, gen_value_fn
from ._engine import Monitor
from ._expr import ConstRef, SignalRef
from ._parser import parse_assertion
from ._prop import FAIL, PASS


class TraceBinder:
    def __init__(self, signals: dict, params: dict | None = None):
        # signals: name -> width or (width, signed)
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
            return None
        w, s = self.signals[name]
        slot = self.slot_index.get(name)
        if slot is None:
            slot = self.slot_index[name] = len(self.slot_names)
            self.slot_names.append(name)
        return SignalRef(slot, w, s, label=name)

    def suggest(self, path):
        import difflib
        return difflib.get_close_matches(".".join(path), list(self.signals), n=3)

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

    def now(self):
        return self.tickno


def run_trace(assertions: dict, signals: dict, rows: list, params: dict | None = None,
              named: dict | None = None, kinds: dict | None = None, initial: dict | None = None,
              finish: bool = True, once=()) -> TraceResult:
    """Evaluate assertions {name: text} on rows. Ticks are numbered from 0.

    Assertions named in `once` start a single attempt at the first tick (SV:
    a concurrent assertion in an initial block)."""
    binder = TraceBinder(signals, params)
    named_defs = {}
    for n, d in (named or {}).items():
        named_defs[n] = d if isinstance(d, NamedDef) else NamedDef(n, _parse_seq(d), (), d)
    result = TraceResult()
    result.warnings = []
    sink = _Sink(result)
    dom = _Domain()
    mons = []
    for name, text in assertions.items():
        spec = parse_assertion(text)
        prog = Compiler(binder, text, named_defs).compile(spec, (kinds or {}).get(name, "assert"))
        result.warnings += prog.warnings
        mons.append(Monitor(prog, name, sink, dom))
    nslots = len(binder.slot_names)
    nx = len(binder.xslot_names)
    names = binder.slot_names
    xnames = binder.xslot_names
    row = {}
    R = [(lambda n=n: row.get(n, 0)) for n in names]
    RX = [(lambda n=n: row.get((n, "x"), 0)) for n in xnames]
    # history channels primed with the initial row (None if not given)
    from collections import deque
    hist = []
    for key, vfn, gfn, depth in binder.channels:
        init = None
        if initial is not None:
            row.clear()
            row.update(initial)
            c0 = [None] * nslots
            init = vfn(c0, R, [None] * nx, RX, [None] * len(binder.channels))
        hist.append(deque([init] * depth, maxlen=depth))
    H = [None] * len(binder.channels)
    for t, r in enumerate(rows):
        dom.tickno = t
        row.clear()
        row.update(r)
        c = [None] * nslots
        cx = [None] * nx
        for k, (key, vfn, gfn, depth) in enumerate(binder.channels):
            H[k] = hist[k][0]
            cur = vfn(c, R, cx, RX, H)
            if gfn is None or gfn(c, R, cx, RX, H):
                hist[k].append(cur)
        for m in mons:
            if m.name in once:
                m.tick_once(c, R, cx, RX, H, t)
            elif m.prog.envmode:
                m.tick_env(c, R, cx, RX, H, t)
            else:
                m.tick(c, R, cx, RX, H, t)
    if finish:
        for m in mons:
            m.finish(len(rows))
    for m in mons:
        result.stats[m.name] = m.stats.as_dict()
    return result


def _parse_seq(text):
    from ._parser import parse_sequence_or_property
    return parse_sequence_or_property(text)
