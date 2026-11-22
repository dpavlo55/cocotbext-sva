"""User-facing API: assertion groups bound to a cocotb design."""
from __future__ import annotations

import difflib
import functools
import inspect
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Callable

import cocotb
from cocotb.triggers import (Event, FallingEdge, First, NextTimeStep, ReadOnly, ReadWrite,
                             RisingEdge, ValueChange)

from . import _ast as A
from ._access import signal_info, x_reader
from ._compile import Compiler, NamedDef, gen_value_fn, substitute
from ._engine import GatedStep, Monitor, build_runner, make_kernel
from ._errors import SvaError, SvaNameError
from ._expr import ConstRef, ExprCompiler, Resolver, SignalRef
from ._multiclock import Rewriter, has_clocks, strip_clocks, uses_clocks
from ._parser import parse_assertion, parse_expression, parse_sequence_or_property
from ._prop import FAIL, PASS

_log = logging.getLogger("sva")

SEVERITIES = ("info", "warning", "error", "fatal")
SAMPLINGS = ("auto", "edge", "preponed")
# Default sampling of a clock (see _Domain): COCOTBEXT_SVA_SAMPLING=auto|edge|preponed
DEFAULT_SAMPLING = os.environ.get("COCOTBEXT_SVA_SAMPLING", "auto").lower()
if DEFAULT_SAMPLING not in SAMPLINGS:
    raise ValueError(f"COCOTBEXT_SVA_SAMPLING must be one of {SAMPLINGS}")
CHECK_TICKS = 256        # auto sampling: ticks compared at the start
RECHECK_EVERY = 4096     # ... then RECHECK_TICKS ticks are compared every so many ticks
RECHECK_TICKS = 16


# --------------------------------------------------------------------------
# Clocking events
# --------------------------------------------------------------------------

class ClockingEvent:
    """A clock signal, an edge and an optional `iff` condition.

    `sampling` selects how values are sampled at a tick of this clock:
    "edge" reads them when the clock edge is seen (exact for clocks driven
    from cocotb), "preponed" takes them at the start of the time step (exact
    for every clock, including clocks generated or derived inside the HDL, at
    a higher cost per cycle), "auto" (the default) compares the two for a
    while and keeps "edge" unless they differ.
    """

    def __init__(self, signal, edge: str = "posedge", iff: str | None = None,
                 sampling: str | None = None):
        if edge not in ("posedge", "negedge", "edge"):
            raise ValueError("edge must be 'posedge', 'negedge' or 'edge'")
        if sampling is not None and sampling not in SAMPLINGS:
            raise ValueError(f"sampling must be one of {SAMPLINGS}")
        self.signal = signal
        self.edge = edge
        self.iff_text = iff
        self.sampling = sampling

    def iff(self, condition: str) -> "ClockingEvent":
        """Qualify the clock: ticks only count when `condition` is true (like `@(posedge clk iff en)`)."""
        return ClockingEvent(self.signal, self.edge, condition, self.sampling)

    def __repr__(self):
        name = getattr(self.signal, "_path", self.signal)
        s = f"{self.edge} {name}"
        return f"@({s} iff {self.iff_text})" if self.iff_text else f"@({s})"


def posedge(signal, sampling: str | None = None) -> ClockingEvent:
    return ClockingEvent(signal, "posedge", sampling=sampling)


def negedge(signal, sampling: str | None = None) -> ClockingEvent:
    return ClockingEvent(signal, "negedge", sampling=sampling)


def edge(signal, sampling: str | None = None) -> ClockingEvent:
    return ClockingEvent(signal, "edge", sampling=sampling)


# --------------------------------------------------------------------------
# Reusable properties and sequences
# --------------------------------------------------------------------------

class Property:
    """A property (or sequence) with formal arguments, like an SV `property ... endproperty`.

        VALID_HELD = Property("valid && !ready |=> valid && $stable(data)",
                              args=("valid", "ready", "data"))
        a.assert_property(VALID_HELD(valid="in_valid", ready="in_ready", data=dut.in_data))

    Actual arguments are names or expressions (strings) resolved in the
    assertion's scope, or signal handles.
    """

    def __init__(self, text: str, args: tuple = ()):
        self.text = text
        self.args = tuple(args)
        self.ast = parse_sequence_or_property(text)

    def __call__(self, *pos, **kw) -> "BoundProperty":
        if len(pos) > len(self.args):
            raise TypeError(f"too many arguments (property takes {len(self.args)})")
        bindings = dict(zip(self.args, pos))
        for k, v in kw.items():
            if k not in self.args:
                raise TypeError(f"unknown argument '{k}' (expected one of {self.args})")
            bindings[k] = v
        missing = [a for a in self.args if a not in bindings]
        if missing:
            raise TypeError(f"missing argument(s): {', '.join(missing)}")
        return BoundProperty(self, bindings)


@dataclass
class BoundProperty:
    prop: Property
    bindings: dict

    @property
    def text(self):
        return self.prop.text


Sequence = Property


# --------------------------------------------------------------------------
# Failures and results
# --------------------------------------------------------------------------

@dataclass
class Failure:
    """One reported failure (several merged attempts are reported once with count > 1)."""
    name: str
    text: str
    time: float                  # simulation time of the failure, in ns
    start_times: list            # start times of the failing attempts (ns), oldest first
    count: int = 1
    severity: str = "error"
    message: str = ""
    values: dict = field(default_factory=dict)
    at_end: bool = False         # strong obligation still pending at the end
    immediate: bool = False

    def __str__(self):
        when = "at end of test" if self.at_end else f"at {self.time:g} ns"
        return f"{self.name} failed {when}: {self.text}"


def _now_ns(steps: int) -> float:
    from cocotb.utils import get_time_from_sim_steps
    return get_time_from_sim_steps(steps, "ns")


def _sim_steps() -> int:
    from cocotb.simtime import get_sim_time
    return get_sim_time("step")


# --------------------------------------------------------------------------
# Name resolution against a cocotb hierarchy
# --------------------------------------------------------------------------

def _child(handle, name: str):
    for cand in (name, name.lower(), name.upper()):
        try:
            return getattr(handle, cand)
        except AttributeError:
            continue
    return None


def _names_under(handle) -> list:
    try:
        return [c._name for c in handle]
    except Exception:
        return []


class _Domain:
    """All assertions sampled on one clocking event share one loop and one value cache.

    The loop is a callback on cocotb's edge trigger of the clock (re-armed at
    every edge), not a task: it skips the task resume, the largest fixed cost
    per tick. Trigger callbacks run before any task resumed by the same edge,
    so the values read are never affected by writes a test makes at that edge.
    An exception raised in it (fatal severity, user callbacks) is handed to a
    small relay task that re-raises it in the test.

    Idle sleep: after a tick where every assertion is idle (nothing in flight,
    and the next ticks would only be counted, not reported) and the $past
    histories are steady, the clock callback only counts edges. Value-change
    callbacks on every signal the idle states read wake the domain up; the
    counted ticks are then applied exactly (attempt and vacuity counters, tick
    numbers). Disable with COCOTBEXT_SVA_IDLE_SLEEP=0.

    Sampling. "edge": values are read in the clock callback. That is the
    value before the edge for clocks driven from cocotb, on every simulator,
    but not for clocks generated or derived inside the HDL (signals of the
    source domain have changed already). "preponed": a callback at the start
    of every time step (re-armed there) reads every signal of the domain
    unless the clock already has its active level, and a tick uses the
    snapshot of its own time step: the values from before the time step, which
    is what SVA samples, whatever drives the clock. "auto": preponed for the
    first CHECK_TICKS ticks, comparing the snapshot with the values at the
    edge; it becomes "preponed" at the first difference (that tick already
    uses the snapshot), "edge" otherwise, and then compares RECHECK_TICKS
    ticks again every RECHECK_EVERY ticks.
    """

    def __init__(self, key, clock_handle, edge_kind: str, label: str, sampling: str = "edge"):
        self.sampling = sampling
        self._pre = sampling != "edge"  # snapshots are being taken
        self._check = CHECK_TICKS if sampling == "auto" else 0
        self._recheck = 0              # edge ticks until the next comparison (auto)
        self._late = False             # comparing again after ticks that were not compared
        self._sc: list = []            # snapshot of c / cx at the start of time step _snap_t
        self._scx: list = []
        self._sig: list = []           # slots that hold signals (not Python values)
        self._snap_t = None
        self._snapped = False          # the last tick used a snapshot
        self._nts_armed = False
        self._step_cb = None           # armed start-of-step / read-write callbacks
        self._rw_cb = None
        self._wake_t = None            # time step of a wake-up by a signal change
        self._lvl = None               # (clock reader, level at which no tick can follow)
        self._watch_slots = ((), ())
        self.unsnapped = 0             # statistics: preponed ticks without a snapshot
        self.wake_ticks = 0            # ... ticks in the time step of a wake-up, counted as slept
        self.key = key
        self.clock_handle = clock_handle
        self.edge = edge_kind
        self.label = label
        self.slot_of: dict = {}
        self.R: list = []
        self.labels: list = []
        self.handles: list = []        # slot -> signal handle (None: Python value)
        self.xslot_of: dict = {}
        self.RX: list = []
        self.xhandles: list = []       # X slot -> signal handle
        self.channels: list = []       # (vfn, gfn, ring)
        self.channel_slots: list = []  # (slots, xslots) read by each channel
        self.channel_of: dict = {}
        self.monitors: list = []
        self.steps: list = []          # bound step functions of the monitors (hot loop)
        self.removals: list = []       # handles to detach after the current tick
        self.iff_fn = None
        self.iff_slots = (frozenset(), frozenset())
        self.tickno = -1
        self.ev = -1                   # evaluated ticks - 1 (edges whose iff held)
        self._run = None               # generated per-tick function (None: rebuild)
        self._busy = 0
        self._kd = False               # compiled kernel (None: not used; False: not created yet)
        self._watch = None             # cached _watch_set()
        self._now_tick = None
        self._now = 0
        self.task = None
        self.c: list = []
        self.cx: list = []
        self.H: list = []
        self.idle_sleep = IDLE_SLEEP
        self.sleeping = False
        self.skipped = 0               # edges counted while asleep
        self.sleeps = 0                # statistics: times the domain went to sleep
        self.stopped = True
        self.error = None
        self._cb = None                # armed clock callback (one-shot, re-armed each edge)
        self._armed: set = set()       # signals with an armed wake-up callback
        self._err = None
        self._trig = None              # edge trigger of the clock (created on start)

    # slots
    def slot(self, handle, info) -> int:
        key = getattr(handle, "_handle", handle)
        s = self.slot_of.get(key)
        if s is None:
            s = self.slot_of[key] = len(self.R)
            self.R.append(info.reader)
            self.labels.append(info.label)
            self.handles.append(handle)
            self.c.append(None)
        return s

    def value_slot(self, key, fn, label) -> int:
        s = self.slot_of.get(key)
        if s is None:
            s = self.slot_of[key] = len(self.R)
            self.R.append(fn)
            self.labels.append(label)
            self.handles.append(None)
            self.c.append(None)
        return s

    def xslot(self, handle) -> int:
        key = getattr(handle, "_handle", handle)
        s = self.xslot_of.get(key)
        if s is None:
            s = self.xslot_of[key] = len(self.RX)
            from cocotb.handle import EnumObject, IntegerObject
            self.RX.append((lambda: 0) if isinstance(handle, (IntegerObject, EnumObject)) else x_reader(handle))
            self.xhandles.append(handle)
            self.cx.append(None)
        return s

    def channel(self, key, code, width, gcode, depth) -> int:
        k = self.channel_of.get(key)
        if k is None:
            k = self.channel_of[key] = len(self.channels)
            vfn = gen_value_fn(code, width)
            gfn = gen_value_fn(gcode) if gcode is not None else None
            # Prime the history with the value at creation time ("default sampled value").
            self.H.append(None)
            c = [None] * len(self.R)
            cx = [None] * len(self.RX)
            try:
                init = vfn(c, self.R, cx, self.RX, self.H)
            except Exception:
                init = None
            self.channels.append((vfn, gfn, deque([init] * depth, maxlen=depth)))
            sl, xs = set(code.slots), set(code.xslots)
            if gcode is not None:
                sl |= gcode.slots
                xs |= gcode.xslots
            self.channel_slots.append((sl, xs))
        return k

    def endpoint(self, key, fn) -> int:
        """Channel computed at each tick before the assertions (ring None):
        the end points of a sequence, for `.triggered`. The engine state it
        holds changes at every tick, so the domain does not sleep."""
        k = self.channel_of.get(key)
        if k is None:
            k = self.channel_of[key] = len(self.channels)
            self.H.append(0)
            self.channels.append((fn, None, None))
            self.channel_slots.append((set(), set()))
            self.idle_sleep = False
        return k

    def now(self) -> int:
        if self._now_tick != self.tickno:
            self._now_tick = self.tickno
            self._now = _sim_steps()
        return self._now

    def detach(self, h):
        if h in self.monitors:
            self.monitors.remove(h)
            self.steps.remove(h.step)
            self._run = None

    # -- loop ---------------------------------------------------------------
    def start(self):
        if self.task is None or self.task.done():
            self.stopped = False
            self.error = None
            self._err = Event()
            self.task = cocotb.start_soon(self._relay())
            self._arm()
            if self._pre:
                ll = getattr(self.clock_handle, "_handle", None)
                level = {"posedge": "1", "negedge": "0"}.get(self.edge)
                if ll is not None and level is not None:
                    self._lvl = (ll.get_signal_val_binstr, level)
                self._arm_step()
                self._snapshot()
        else:
            self.wake()

    async def _relay(self):
        try:
            await self._err.wait()
            raise self.error
        finally:
            self._stop()
            for m in self.monitors:
                m.group._task_ended()

    def _arm(self):
        if self._trig is None:
            self._trig = {"posedge": RisingEdge, "negedge": FallingEdge}.get(self.edge, ValueChange)(
                self.clock_handle)
        self._cb = self._trig._register(self._on_clock)

    def _stop(self):
        self.stopped = True
        if self._cb is not None:
            self._cb.cancel()
            self._cb = None
        # A step callback left armed at the end of the simulation crashes GHDL
        # and Verilator (see _arm_step).
        for attr in ("_step_cb", "_rw_cb"):
            cb = getattr(self, attr)
            if cb is not None:
                cb.cancel()
                setattr(self, attr, None)
        self._nts_armed = False
        self._unwatch()
        self.sleeping = False

    # -- preponed sampling ------------------------------------------------------
    def _arm_step(self):
        """Start the chain of step callbacks: read-write phase of this time
        step -> start of the next one -> its read-write phase -> ...

        A start-of-step callback is only ever registered from the read-write
        phase, so none is pending when a test ends at a clock edge or a
        timer. Measured (phase4-results.md): on Verilator, removing one that
        was registered from inside a start-of-step callback crashes the
        simulator, and so does ending the simulation in a time step in which
        one was registered, removed or not; GHDL crashes at the end if one is
        left armed. The read-only phase cannot be the link: waiting for it
        makes GHDL run pending `after` delays early."""
        if not self._nts_armed:
            self._nts_armed = True
            self._rw_cb = ReadWrite()._register(self._on_step_end)

    def _on_step(self):
        """Start of a time step: take the values a tick in this time step samples."""
        self._step_cb = None
        if self.stopped or self.sleeping or not self._pre:
            self._nts_armed = False
            return
        self._rw_cb = ReadWrite()._register(self._on_step_end)
        lvl = self._lvl
        if lvl is not None and lvl[0]() == lvl[1]:
            return                     # the clock cannot tick in this time step
        self._snapshot()

    def _on_step_end(self):
        self._rw_cb = None
        if self.stopped or self.sleeping or not self._pre:
            self._nts_armed = False
            return
        self._step_cb = NextTimeStep()._register(self._on_step)

    def _snapshot(self):
        R, RX, sc, scx = self.R, self.RX, self._sc, self._scx
        n = len(R)
        if len(sc) != n:
            sc.extend([None] * (n - len(sc)))
            self._sig = [i for i in range(n) if self.handles[i] is not None]
        for i in self._sig:
            sc[i] = R[i]()
        if RX:
            if len(scx) != len(RX):
                scx.extend([None] * (len(RX) - len(scx)))
            for i in range(len(RX)):
                scx[i] = RX[i]()
        self._snap_t = _sim_steps()

    def _load(self) -> bool:
        """Fill the value cache for a tick in preponed mode; False if the tick
        was accounted for as a slept one."""
        c, cx, sc, scx = self.c, self.cx, self._sc, self._scx
        now = _sim_steps()
        if self._snap_t == now:
            if len(sc) != len(c):      # signals added since the snapshot: read at the edge
                sc.extend([None] * (len(c) - len(sc)))
                self._sig = [i for i in range(len(c)) if self.handles[i] is not None]
            if len(scx) != len(cx):
                scx.extend([None] * (len(cx) - len(scx)))
            if self._check:
                self._compare()
            c[:] = sc
            cx[:] = scx
            self._snapped = True
            return True
        if self._wake_t == now:
            # Woken in this time step by a change of a watched signal: before
            # the time step the values were those the domain slept on.
            self._wake_t = None
            self.wake_ticks += 1
            self._slept(1)
            return False
        self.unsnapped += 1
        self._snapped = False
        for i in range(len(c)):
            c[i] = None
        for i in range(len(cx)):
            cx[i] = None
        return True

    def _compare(self):
        """auto sampling: do reads at the edge still equal the snapshot?"""
        R, RX, sc, scx = self.R, self.RX, self._sc, self._scx
        diff = None
        for i in self._sig:
            if sc[i] is not None and R[i]() != sc[i]:
                diff = self.labels[i]
                break
        else:
            for i in range(len(RX)):
                if scx[i] is not None and RX[i]() != scx[i]:
                    diff = getattr(self.xhandles[i], "_path", "?")
                    break
        if diff is not None:
            self.sampling = "preponed"
            self._check = 0
            if self._late:
                _log.warning("clock %s: '%s' had already changed when the clock edge was seen "
                             "(clock generated or derived inside the HDL?). Switching to preponed "
                             "sampling; this was first seen after %d ticks, so earlier results on "
                             "this clock may have used values from after the edge. Select "
                             "sampling='preponed' for this clock.", self.label, diff, self.tickno + 1)
            else:
                _log.info("clock %s: '%s' had already changed when the clock edge was seen (clock "
                          "generated or derived inside the HDL?): using preponed sampling for "
                          "this clock (sampling='preponed' selects it from the start)",
                          self.label, diff)
            return
        self._check -= 1
        if not self._check:
            self.sampling = "edge"
            self._pre = False          # the step callback stops at its next call
            self._recheck = RECHECK_EVERY
            self._late = True

    def _on_clock(self):
        if self.stopped:
            self._cb = None
            return
        self._arm()                    # the fired callback is gone: arm the next edge
        if self.sleeping:
            self.skipped += 1
            return
        try:
            if self._tick() and self.idle_sleep and not self._busy:
                self._maybe_sleep()
        except BaseException as e:     # noqa: B036 - relayed to the test
            self.error = e
            self._stop()
            self._err.set()

    def _tick(self) -> bool:
        """Evaluate one clock edge; False if the clocking event's iff was false."""
        c, cx, H, R, RX = self.c, self.cx, self.H, self.R, self.RX
        if self._pre:
            if not self._load():
                return False
        else:
            for i in range(len(c)):
                c[i] = None
            for i in range(len(cx)):
                cx[i] = None
            if self._recheck:
                self._recheck -= 1
                if not self._recheck:  # auto: compare the next ticks again
                    self._pre = True
                    self._check = RECHECK_TICKS
                    self._arm_step()
        self.tickno += 1
        if self.iff_fn is not None and not self.iff_fn(c, R, cx, RX, H):
            return False
        self._evaluate()
        return True

    def _evaluate(self):
        """Step the histories and the assertions on the values in the cache."""
        c, cx, H, R, RX = self.c, self.cx, self.H, self.R, self.RX
        for k, (vfn, gfn, ring) in enumerate(self.channels):
            if ring is None:           # end points of a sequence (.triggered)
                H[k] = vfn(c, R, cx, RX, H)
                continue
            H[k] = ring[0]
            cur = vfn(c, R, cx, RX, H)
            if gfn is None or gfn(c, R, cx, RX, H):
                ring.append(cur)
        self.ev += 1
        run = self._run
        if run is None:
            if self._kd is False:
                self._kd = make_kernel(self)
            run = self._run = build_runner([(h.monitor, h.step) for h in self.monitors], self._kd)
            self._watch = None
        # fast-path monitors that were not quiet this tick: no sleep yet
        self._busy = run(c, R, cx, RX, H, self.tickno)
        if self.removals:
            for hd in self.removals:
                self.detach(hd)
            self.removals.clear()

    # -- idle sleep -----------------------------------------------------------
    def _maybe_sleep(self):
        """Called after a tick where the runner called no fast-path monitor:
        those are all quiet, hence sleepable; check the others."""
        run = self._run
        if run is None:
            return                     # assertions removed this tick: try next tick
        for m in run.others:
            if not m.sleepable():
                return
        # The histories must be steady, and equal to what this tick read
        # (H[k], taken before this tick's value was appended): the skipped
        # ticks are counted with this tick's outcome, so they must see the
        # same past values. (A history that just changed, e.g. $rose true on
        # this tick, would otherwise be counted as true while asleep.)
        H = self.H
        for k, (_, _, ring) in enumerate(self.channels):
            if ring.count(H[k]) != len(ring):
                return
        watch = self._watch
        if watch is None:
            watch = self._watch = self._watch_set()
        if not watch:
            return                     # a Python value: changes cannot be observed
        if self._pre:
            # The tick used values from before this time step. A watched
            # signal that has changed since would change without waking the
            # domain: stay awake.
            if not self._snapped:
                return
            R, RX, sc, scx = self.R, self.RX, self._sc, self._scx
            sl, xs = self._watch_slots
            for i in sl:
                if R[i]() != sc[i]:
                    return
            for i in xs:
                if RX[i]() != scx[i]:
                    return
        self.sleeping = True
        self.sleeps += 1
        self.skipped = 0
        # Wake-up callbacks are never cancelled (cancelling value-change
        # callbacks from inside one hangs some simulators). At most one is
        # armed per signal; one left from an earlier sleep stays valid, and a
        # wake-up that turns out unnecessary only costs one evaluated tick.
        for key, hd in watch.items():
            if key not in self._armed:
                self._armed.add(key)
                ValueChange(hd)._register(functools.partial(self._on_wake, key))

    def _watch_set(self):
        """Signals whose changes end idle sleep ({key: handle}), or False if
        an idle state reads a Python value. Fixed while the set of assertions
        is (the idle states read the same inputs every time)."""
        sl, xs = set(self.iff_slots[0]), set(self.iff_slots[1])
        for h in self.monitors:
            a, b = h.monitor.idle_slots()
            sl |= a
            xs |= b
        for a, b in self.channel_slots:
            sl |= a
            xs |= b
        watch = {}
        for s_ in sl:
            hd = self.handles[s_]
            if hd is None:
                return False
            watch[id(hd._handle)] = hd
        for x in xs:
            hd = self.xhandles[x]
            watch[id(hd._handle)] = hd
        self._watch_slots = (sorted(sl), sorted(xs))
        return watch

    def _on_wake(self, key):
        self._armed.discard(key)
        if self.sleeping and not self.stopped:
            self.wake(changed=True)

    def _unwatch(self):
        pass                           # see _maybe_sleep: wake-ups are left armed

    def wake(self, changed: bool = False):
        """Leave idle sleep, applying the edges counted meanwhile.

        changed: called because a watched signal changed, or with nothing
        else changing (preponed sampling: a tick later in this time step is
        then one more slept tick; otherwise it uses the values read now)."""
        if not self.sleeping:
            return
        self.sleeping = False
        self._unwatch()
        k, self.skipped = self.skipped, 0
        self._slept(k)
        if self._pre:
            self._arm_step()
            if changed:
                self._wake_t = _sim_steps()
            else:
                self._wake_t = None
                self._snapshot()

    def _slept(self, k):
        if k:
            self.ev += k
            kd = self._kd
            if kd:
                kd.slept(k)            # every kernel monitor in one call
            for h in self.monitors:
                if h.monitor._k is None:
                    h.monitor.slept(k)
            self.tickno += k
            # the histories are steady: appending the same value changes nothing


def _edge_trigger(handle, edge_kind):
    return {"posedge": RisingEdge, "negedge": FallingEdge}.get(edge_kind, ValueChange)(handle)


class _MultiDomain(_Domain):
    """The domain of multi-clock assertions on one set of clocks.

    It ticks once per time step in which at least one of the clocks ticks
    (the assertions are rewritten over that tick stream, see _multiclock).
    Sampling is preponed: all the clock edges of a time step see the same
    values. The edge callbacks only note which clocks ticked; the tick is
    evaluated in the read-only phase of the time step, when every edge has
    been seen (failure callbacks therefore cannot write signals). On GHDL it
    is evaluated in the read-write phase instead, because waiting for the
    read-only phase disturbs the simulation there (see _arm_step); an edge
    that came after that phase would be a tick of its own.

    No idle sleep."""

    def __init__(self, key, clocks):
        # clocks: [(handle, edge kind, label)]
        label = " | ".join(c[2] for c in clocks)
        super().__init__(key, clocks[0][0], clocks[0][1], label, "preponed")
        self.idle_sleep = False
        self.clocks = clocks
        n = len(clocks)
        self.iff_fns = [None] * n
        self._seen = [0] * n            # clocks whose edge was seen in this time step
        self._flags = [0] * n           # clocks that tick in the tick being evaluated
        self._pending = False
        self._eval_cb = None            # armed read-only callback
        self._at_rw = "ghdl" in getattr(cocotb, "SIM_NAME", "").lower()
        self._edge_cbs = [None] * n
        self._trigs = [None] * n
        self.tick_slots = [self.value_slot(("tick", i), (lambda i=i: self._flags[i]), "@" + clocks[i][2])
                           for i in range(n)]

    def start(self):
        if self.task is None or self.task.done():
            self.stopped = False
            self.error = None
            self._err = Event()
            self.task = cocotb.start_soon(self._relay())
            for i, (handle, edge_kind, _) in enumerate(self.clocks):
                self._trigs[i] = _edge_trigger(handle, edge_kind)
                self._edge_cbs[i] = self._trigs[i]._register(functools.partial(self._on_edge, i))
            self._arm_step()
            self._snapshot()

    def _stop(self):
        for i, cb in enumerate(self._edge_cbs):
            if cb is not None:
                cb.cancel()
                self._edge_cbs[i] = None
        if self._eval_cb is not None:
            self._eval_cb.cancel()
            self._eval_cb = None
        super()._stop()

    def _on_edge(self, i):
        if self.stopped:
            self._edge_cbs[i] = None
            return
        self._edge_cbs[i] = self._trigs[i]._register(functools.partial(self._on_edge, i))
        self._seen[i] = 1
        self._pending = True
        if self._at_rw:
            if self._rw_cb is None:    # (an edge after this time step's read-write callback)
                self._rw_cb = ReadWrite()._register(self._on_step_end)
        elif self._eval_cb is None:
            self._eval_cb = ReadOnly()._register(self._on_eval)

    def _on_eval(self):
        self._eval_cb = None
        if self._pending and not self.stopped:
            self._flush()

    def _on_step_end(self):
        self._rw_cb = None
        if self._at_rw and self._pending and not self.stopped:
            self._flush()
        if self.stopped:
            self._nts_armed = False
        elif self._step_cb is None:
            self._step_cb = NextTimeStep()._register(self._on_step)

    def _flush(self):
        """Evaluate the tick of this time step."""
        self._pending = False
        try:
            self._tick()
        except BaseException as e:     # noqa: B036 - relayed to the test
            self.error = e
            self._stop()
            self._err.set()

    def _tick(self) -> bool:
        flags, self._seen = self._seen, [0] * len(self.clocks)
        self._flags = flags
        self._load()
        c, cx, H, R, RX = self.c, self.cx, self.H, self.R, self.RX
        for i, fn in enumerate(self.iff_fns):
            if flags[i] and fn is not None and not fn(c, R, cx, RX, H):
                flags[i] = 0
        if not any(flags):
            return False
        self.tickno += 1
        self._evaluate()
        return True

    def wake(self, changed: bool = False):
        # Called before results are read or assertions change: a tick whose
        # edges were seen in this time step is evaluated first.
        if self._pending and not self.stopped:
            self._flush()


_domains: dict = {}
IDLE_SLEEP = os.environ.get("COCOTBEXT_SVA_IDLE_SLEEP", "1") not in ("0", "", "false", "no")


def _get_domain(clock_handle, edge_kind, iff_text, scope_key, label, sampling="edge") -> _Domain:
    key = (getattr(clock_handle, "_handle", clock_handle), edge_kind, iff_text,
           scope_key if iff_text else None, sampling)
    d = _domains.get(key)
    if d is None or (d.task is not None and d.task.done()):
        d = _domains[key] = _Domain(key, clock_handle, edge_kind, label, sampling)
    return d


_TICK = "$tick"      # name of the tick signal of clock i of a multi-clock domain: $tick<i>


class _Binder(Resolver):
    """Resolves names for one assertion: overrides, constants, then the scope hierarchy."""

    def __init__(self, group: "Assertions", domain: _Domain | None, signals: dict, values: dict):
        self.group = group
        self.domain = domain
        self.signals = signals
        self.values = values

    def lookup(self, path):
        name = ".".join(path)
        g = self.group
        if name.startswith(_TICK):
            slot = self.domain.tick_slots[int(name[len(_TICK):])]
            return SignalRef(slot, 1, False, label=self.domain.labels[slot])
        if name in self.signals:
            return self._signal_ref(self.signals[name], name)
        if name in self.values:
            fn = self.values[name]
            width = 64
            if isinstance(fn, tuple):
                fn, width = fn
            m = (1 << width) - 1
            slot = self.domain.value_slot(("value", id(fn)), lambda: int(fn()) & m, name)
            return SignalRef(slot, width, False, label=name)
        for consts in g._const_chain():
            if name in consts:
                v = consts[name]
                return ConstRef(int(v))
        handle = g._resolve_path(path)
        if handle is None:
            return None
        return self._signal_ref(handle, name)

    def _signal_ref(self, handle, name):
        if isinstance(handle, str):
            raise SvaError(f"'{name}' is bound to an expression; use it as a port of a Checker or Property")
        info = signal_info(handle, name)
        slot = self.domain.slot(handle, info)
        self.group._handles_by_slot[(id(self.domain), slot)] = handle
        return SignalRef(slot, info.width, info.signed, info.left, info.right, label=name)

    def suggest(self, path):
        return self.group._suggest(path)

    # Resolver interface for expressions compiled outside a property
    # (immediate assertions, clock `iff` conditions).
    def resolve(self, ident):
        ref = self.lookup(ident.path)
        if ref is None:
            s = self.suggest(ident.path)
            hint = f" (did you mean {', '.join(repr(x) for x in s)}?)" if s else ""
            raise SvaNameError(f"unknown name '{ident.name}'{hint}")
        return ref

    def local_width(self, ref):
        raise SvaError("local variables are only available in properties")

    def triggered_channel(self, name):
        raise SvaError("'.triggered' is only available in properties")

    def x_slot(self, ref):
        handle = self.group._handles_by_slot.get((id(self.domain), ref.slot))
        if handle is None:
            raise SvaError(f"X detection is not available for '{ref.label}'")
        return self.domain.xslot(handle)

    def endpoint_channel(self, key, fn):
        if self.domain is None or self.domain.key is None:
            raise SvaError("'.triggered' needs a clock (not available in immediate assertions)")
        return self.domain.endpoint(key, fn)

    def history_channel(self, key, code, width, gcode, depth):
        if self.domain is None:
            raise SvaError("sampled-value functions need a clock (not available in immediate assertions)")
        return self.domain.channel(key, code, width, gcode, depth)


# --------------------------------------------------------------------------
# Assertion instances
# --------------------------------------------------------------------------

class AssertionHandle:
    """An assertion attached to a clocking domain (returned by assert_property & co.)."""

    def __init__(self, group, name, prog, domain, severity, message, on_fail, on_pass,
                 initial: bool = False):
        self.group = group
        self.name = name
        self.prog = prog
        self.domain = domain
        self.severity = severity
        self.message = message
        self.on_fail = on_fail
        self.on_pass = on_pass
        self.kind = prog.kind
        # read by the Monitor when it is created
        self.wants_pass = on_pass is not None or prog.kind == "cover"
        self.wants_vacuous = False
        self.monitor = Monitor(prog, name, self, domain)
        self.enabled_any = True
        self.initial = initial
        if initial:
            self.step = self.monitor.tick_once
        else:
            self.step = self.monitor.tick_env if prog.envmode else self.monitor.tick
        self.hits = 0

    @property
    def stats(self) -> dict:
        self.domain.wake(True)      # apply ticks counted during idle sleep
        d = self.monitor.stats.as_dict()
        if self.kind == "cover":
            d = {"attempts": d["attempts"], "hits": d["passes"]}
        return d

    @property
    def enabled(self) -> bool:
        return self.monitor.enabled

    # sink protocol used by the engine
    def __call__(self, verdict, mon, count, starts, tickno, at_end=False):
        if verdict == PASS:
            self.hits += count
            if self.on_pass is not None:
                self.on_pass(self)
            return
        if verdict != FAIL:
            return
        self.group._report_failure(self, count, starts, at_end)


# --------------------------------------------------------------------------
# Assertion groups
# --------------------------------------------------------------------------

_test_groups: list = []        # groups created in the current @checked test


class Assertions:
    """A group of assertions on one design scope.

    Args:
        scope: hierarchy handle (usually `dut`) in which bare names are looked up.
        clock: default clocking event: `posedge(dut.clk)`, a clock handle (posedge),
            or a string such as "posedge clk" / "@(negedge clk iff en)".
        disable_iff: default `disable iff` condition (SVA expression string).
        name: prefix for assertion names in reports.
        params: constants usable in assertions (`DEPTH`, ...).
        enums: an `enum.IntEnum` class or a dict of enumeration literals
            (`state == S_BUSY`); values are what cocotb reads for the signal.
        signals: name -> handle aliases usable in every assertion of the group.
        sampling: "auto" (default), "edge" or "preponed": how values are sampled
            at a clock tick (see ClockingEvent). A clocking event's own
            sampling= takes precedence.
    """

    def __init__(self, scope=None, clock=None, disable_iff: str | None = None, name: str = "sva",
                 params: dict | None = None, enums=None, signals: dict | None = None,
                 lets: dict | None = None, parent: "Assertions | None" = None,
                 sampling: str | None = None):
        if sampling is not None and sampling not in SAMPLINGS:
            raise ValueError(f"sampling must be one of {SAMPLINGS}")
        self.sampling = sampling
        self.scope = scope
        self.clock = clock
        self.disable_iff = disable_iff
        self.name = name
        self.params = dict(params or {})
        if enums is not None:
            self.params.update(_enum_dict(enums))
        self.signals = dict(signals or {})
        self.lets = {k: parse_sequence_or_property(v) if isinstance(v, str) else v
                     for k, v in (lets or {}).items()}
        self.parent = parent
        self.root = parent.root if parent else self
        self.named: dict = dict(parent.named) if parent else {}
        if parent is None:
            self.assertions: list[AssertionHandle] = []
            self.failures: list[Failure] = []
            self.immediate_stats: dict = {}
            self._handles_by_slot: dict = {}
            self._finalized = False
            self._deferred: dict = {}
            self._deferred_task = None
            self._unchecked_warned = False
            self._count = 0
            _test_groups.append(self)
        self._imm_cache: dict = {}

    # -- configuration helpers ------------------------------------------------
    def _const_chain(self):
        g = self
        while g is not None:
            yield g.params
            g = g.parent

    @property
    def _handles_by_slot(self):
        return self.root.__dict__["_handles_by_slot"] if self.root is not self else self.__dict__["_handles_by_slot"]

    @_handles_by_slot.setter
    def _handles_by_slot(self, v):
        self.__dict__["_handles_by_slot"] = v

    def _resolve_path(self, path):
        if self.scope is None:
            return None
        h = self.scope
        for part in path:
            h = _child(h, part)
            if h is None:
                return None
        return h

    def _suggest(self, path):
        h = self.scope
        for part in path[:-1]:
            h = _child(h, part) if h is not None else None
        names = list(self.signals) + (_names_under(h) if h is not None else [])
        return difflib.get_close_matches(path[-1], names, n=3)

    def _full_name(self, name):
        return f"{self.name}.{name}" if self.name else name

    def sequence(self, name: str, text: str, args: tuple = ()):
        """Define a named sequence usable by name in later assertions (`burst4 ##1 idle`)."""
        self.named[name] = NamedDef(name, parse_sequence_or_property(text), tuple(args), text)

    def let(self, name: str, text: str):
        """Define a named expression (macro), like SV `let`."""
        self.lets[name] = parse_sequence_or_property(text)

    # -- concurrent assertions ----------------------------------------------
    def assert_property(self, prop, name: str | None = None, *, clock=None, disable_iff=...,
                        signals: dict | None = None, values: dict | None = None,
                        severity: str = "error", message: str | None = None,
                        on_fail: Callable | None = None, on_pass: Callable | None = None,
                        initial: bool = False) -> AssertionHandle:
        """Check a concurrent property on every clock tick (SV `assert property`).

        `values` maps names to Python callables sampled at the clock tick.
        `message` may reference signals in braces: "count={u_ctrl.count}".
        With initial=True a single attempt starts at the next clock tick (SV: a
        concurrent assertion in an initial block), e.g. for `always ...`.
        """
        return self._add("assert", prop, name, clock, disable_iff, signals, values, severity, message,
                         on_fail, on_pass, initial)

    def assume_property(self, prop, name: str | None = None, **kw) -> AssertionHandle:
        """SV `assume property` (checked like an assertion in simulation)."""
        kw.setdefault("disable_iff", ...)
        return self._add("assume", prop, name, kw.pop("clock", None), kw.pop("disable_iff"),
                         kw.pop("signals", None), kw.pop("values", None), kw.pop("severity", "error"),
                         kw.pop("message", None), kw.pop("on_fail", None), kw.pop("on_pass", None),
                         kw.pop("initial", False))

    def cover_property(self, prop, name: str | None = None, *, clock=None, disable_iff=...,
                       signals: dict | None = None, values: dict | None = None,
                       on_hit: Callable | None = None, initial: bool = False) -> AssertionHandle:
        """Count matches of a property or sequence (SV `cover property`)."""
        return self._add("cover", prop, name, clock, disable_iff, signals, values, "info", None, None,
                         on_hit, initial)

    async def expect(self, prop, name: str | None = None, *, clock=None, disable_iff=...,
                     signals: dict | None = None, values: dict | None = None,
                     severity: str = "error", message: str | None = None,
                     timeout: int | None = None) -> bool:
        """Wait for one evaluation of a property that starts at the next clock
        tick (SV `expect`). Returns True when it holds; a failure is reported
        like an assertion failure and returns False.

        `timeout` (clock ticks) ends a still-pending evaluation with the
        end-of-test rule: weak obligations hold, strong ones fail.
        """
        h = self._add("assert", prop, name or f"expect{self.root._count + 1}", clock, disable_iff,
                      signals, values, severity, message, None, None, True)
        h.kind = "expect"
        mon = h.monitor
        state = {"ticks": 0, "done": False}
        step = h.step

        def expect_step(c, R, cx, RX, H, t):
            step(c, R, cx, RX, H, t)
            if mon._gated and not mon._att:
                return                 # multi-clock: no tick of the leading clock yet
            state["ticks"] += 1
            if not mon.pending():
                pass
            elif timeout is not None and state["ticks"] >= timeout:
                mon.finish(t)
            else:
                return
            h.domain.removals.append(h)
            state["done"] = True

        h.step = expect_step
        h.domain.steps[h.domain.steps.index(step)] = expect_step
        h.domain._run = None
        # The domain runs in raw simulator callbacks, outside the scheduler:
        # poll on the clock edge instead of waiting on an Event.
        dom = h.domain
        if isinstance(dom, _MultiDomain):
            trigs = [_edge_trigger(hd, e) for hd, e, _ in dom.clocks]
            while not state["done"] and not dom.stopped:
                await First(*trigs)
                dom.wake()             # the tick of this time step
        else:
            trig = _edge_trigger(dom.clock_handle, dom.edge)
            while not state["done"]:
                await trig
                if dom.stopped:
                    break
        return mon.n_failures == 0

    def _add(self, kind, prop, name, clock, disable_iff, signals, values, severity, message,
             on_fail, on_pass, initial=False):
        root = self.root
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        root._count += 1
        name = self._full_name(name or f"{kind}{root._count}")
        overrides = dict(self._signals_chain())
        overrides.update(signals or {})
        text, spec, extra_named = self._prepare(prop, overrides)
        named = dict(self.named)
        named.update(extra_named)
        lead_slot = None
        if uses_clocks(spec.prop, named):
            spec, clock, domain, lead_slot = self._multiclock(spec, clock, overrides, named, kind, text)
        if lead_slot is None:
            clock_ev = self._clock_for(spec.clock, clock, overrides)
            domain = self._domain_for(clock_ev, overrides)
        binder = _Binder(self, domain, overrides, dict(values or {}))
        dis = None
        if disable_iff is ...:
            disable_iff = self._default_disable()
        if disable_iff is not None and spec.disable is None:
            dis = self._expand_lets(parse_expression(disable_iff))
        compiler = Compiler(binder, text, named)
        prog = compiler.compile(spec, kind, default_disable=dis)
        for w in prog.warnings:
            _log.warning("%s: %s", name, w)
        h = AssertionHandle(root, name, prog, domain, severity, message, on_fail, on_pass, initial)
        if lead_slot is not None:
            # attempts start at the ticks of the leading clock only
            h.step = GatedStep(h.monitor, lead_slot, initial)
        h.enabled_any = True
        root.assertions.append(h)
        domain.monitors.append(h)
        domain.steps.append(h.step)
        domain._run = None
        domain.start()
        return h

    def _multiclock(self, spec, clock, overrides, named, kind, text):
        """A property with clocking events of its own. Returns (spec, clock,
        domain, slot of the leading clock's tick). If all of them turn out to
        be one clock: (spec without them, that clock, None, None)."""
        clocks = {}                      # id -> (handle, edge, iff node)

        def clock_id(ev):
            if isinstance(ev, A.ClockEvent):
                sig = self._clock_signal(ev.signal, overrides)
                edge_kind, iff = ev.edge or "edge", ev.iff
            else:
                _, sig, edge_kind, iff, _ = ev
            cid = (getattr(sig, "_handle", sig), edge_kind, _node_text(iff) if iff is not None else None)
            clocks.setdefault(cid, (sig, edge_kind, iff))
            return cid

        default = None
        if spec.clock is not None or clock is not None or self._default_clock() is not None:
            default = clock_id(self._clock_for(spec.clock, clock, overrides))

        def no_tick(cid):
            raise SvaError("no clock: part of the property is not under a clocking event "
                           "(give the group a clock= or pass clock=)")

        const = ExprCompiler(_Binder(self, None, overrides, {}), text).const
        rw = Rewriter(clock_id, no_tick, const, named, kind, text)
        lead, used = rw.analyse(spec.prop, default)
        if None in used:
            no_tick(None)
        if len(used) == 1:
            sig, edge_kind, iff = clocks[lead]
            ev = A.ClockEvent(edge_kind, A.Id(("$clk",)), iff)
            overrides["$clk"] = sig
            for n, d in list(named.items()):     # (named is the caller's own copy)
                if has_clocks(d.ast):
                    named[n] = NamedDef(d.name, strip_clocks(d.ast), d.args, d.text)
            return (A.AssertionSpec(ev, spec.disable, strip_clocks(spec.prop), span=spec.span),
                    None, None, None)
        order = sorted(used, key=lambda cid: (str(getattr(clocks[cid][0], "_path", clocks[cid][0])),
                                              cid[1], cid[2] or ""))
        key = ("multi", tuple(order))
        d = _domains.get(key)
        if d is None or (d.task is not None and d.task.done()):
            d = _domains[key] = _MultiDomain(key, [
                (clocks[cid][0], cid[1],
                 f"{cid[1]} {getattr(clocks[cid][0], '_path', clocks[cid][0])}"
                 + (f" iff {cid[2]}" if cid[2] else "")) for cid in order])
        for i, cid in enumerate(order):
            iff = clocks[cid][2]
            if iff is not None and d.iff_fns[i] is None:
                code = ExprCompiler(_Binder(self, d, overrides, {}), cid[2]).boolean(self._expand_lets(iff))
                d.iff_fns[i] = gen_value_fn(code)
        index = {cid: i for i, cid in enumerate(order)}
        rw.tick = lambda cid: A.Id((f"{_TICK}{index[cid]}",))
        prop, lead = rw.rewrite(spec.prop, default)
        named.update(rw.extra_named)
        return (A.AssertionSpec(None, spec.disable, prop, span=spec.span), None, d,
                d.tick_slots[index[lead]])

    def _signals_chain(self):
        chain = []
        g = self
        while g is not None:
            chain.append(g.signals)
            g = g.parent
        for d in reversed(chain):
            yield from d.items()

    def _default_disable(self):
        g = self
        while g is not None:
            if g.disable_iff is not None:
                return g.disable_iff
            g = g.parent
        return None

    def _prepare(self, prop, overrides):
        """Parse the property text; substitute lets and Property arguments."""
        extra_named = {}
        if isinstance(prop, BoundProperty):
            text = prop.text
            mapping = {}
            for k, v in prop.bindings.items():
                if isinstance(v, str):
                    mapping[k] = parse_sequence_or_property(v)
                else:
                    alias = f"__arg_{k}_{id(v)}"
                    overrides[alias] = v
                    mapping[k] = A.Id((alias,))
            spec = A.AssertionSpec(None, None, substitute(prop.prop.ast, mapping))
        elif isinstance(prop, Property):
            if prop.args:
                raise SvaError("this Property has arguments; call it with actual arguments first")
            text = prop.text
            spec = A.AssertionSpec(None, None, prop.ast)
        else:
            text = prop
            spec = parse_assertion(text)
        lets = self._lets_chain()
        if lets:
            spec = A.AssertionSpec(spec.clock, spec.disable, self._expand_lets(spec.prop), span=spec.span)
        return text, spec, extra_named

    def _lets_chain(self):
        out = {}
        chain = []
        g = self
        while g is not None:
            chain.append(g.lets)
            g = g.parent
        for d in reversed(chain):
            out.update(d)
        return out

    def _expand_lets(self, node):
        lets = self._lets_chain()
        if not lets:
            return node
        return substitute(node, lets)

    def _clock_for(self, spec_clock, clock_arg, overrides):
        if spec_clock is not None:
            sig = self._clock_signal(spec_clock.signal, overrides)
            iff = None
            if spec_clock.iff is not None:
                iff = spec_clock.iff
            return ("ast", sig, spec_clock.edge or "edge", iff, self._default_sampling())
        clk = clock_arg if clock_arg is not None else self._default_clock()
        if clk is None:
            raise SvaError("no clock: give the group a clock=, pass clock= or write @(posedge clk) in the property")
        if isinstance(clk, str):
            text = clk.strip()
            if not text.startswith("@"):
                text = f"@({text})"
            ev = parse_assertion(text + " 1").clock
            return ("ast", self._clock_signal(ev.signal, overrides), ev.edge or "edge", ev.iff,
                    self._default_sampling())
        if isinstance(clk, ClockingEvent):
            iff = parse_expression(clk.iff_text) if clk.iff_text else None
            return ("ast", clk.signal, clk.edge, iff, clk.sampling or self._default_sampling())
        return ("ast", clk, "posedge", None, self._default_sampling())

    def _default_sampling(self):
        g = self
        while g is not None:
            if g.sampling is not None:
                return g.sampling
            g = g.parent
        return DEFAULT_SAMPLING

    def _default_clock(self):
        g = self
        while g is not None:
            if g.clock is not None:
                return g.clock
            g = g.parent
        return None

    def _clock_signal(self, node, overrides):
        if not isinstance(node, A.Id):
            raise SvaError("clock must be a signal name")
        name = node.name
        if name in overrides:
            return overrides[name]
        h = self._resolve_path(node.path)
        if h is None:
            raise SvaNameError(f"unknown clock signal '{name}'")
        return h

    def _domain_for(self, clock_ev, overrides):
        _, sig, edge_kind, iff, sampling = clock_ev
        iff_text = None
        if iff is not None:
            iff_text = _node_text(iff)
        label = f"{edge_kind} {getattr(sig, '_path', sig)}"
        d = _get_domain(sig, edge_kind, iff_text, id(self), label, sampling)
        if iff is not None and d.iff_fn is None:
            binder = _Binder(self, d, overrides, {})
            ec = ExprCompiler(binder, iff_text)
            code = ec.boolean(self._expand_lets(iff))
            d.iff_fn = gen_value_fn(code)
            d.iff_slots = (frozenset(code.slots), frozenset(code.xslots))
        return d

    # -- immediate assertions -----------------------------------------------
    def check(self, expr, msg: str | None = None, *, name: str | None = None, deferred: bool = False,
              severity: str = "error", signals: dict | None = None, values: dict | None = None) -> bool | None:
        """Immediate assertion (SV `assert (expr) else $error(msg)`).

        `expr` is an SVA expression string or a Python callable returning a bool.
        A failure is logged at the current simulation time and collected; the
        test continues. With deferred=True the check runs once at the end of
        the current time step (ReadOnly phase), and only the last call per
        name/call site in that time step counts (SV `assert #0`).
        """
        return self._immediate("assert", expr, msg, name, deferred, severity, signals, values)

    def assume(self, expr, msg: str | None = None, **kw):
        """Immediate assumption (checked like an assertion in simulation)."""
        return self._immediate("assume", expr, msg, kw.get("name"), kw.get("deferred", False),
                               kw.get("severity", "error"), kw.get("signals"), kw.get("values"))

    def cover(self, expr, name: str | None = None, *, signals: dict | None = None,
              values: dict | None = None) -> bool:
        """Immediate cover: counts how often `expr` is true when called."""
        return self._immediate("cover", expr, None, name, False, "info", signals, values)

    def _immediate(self, kind, expr, msg, name, deferred, severity, signals, values):
        root = self.root
        if name is None:
            caller = sys._getframe(2)
            label = expr if isinstance(expr, str) else getattr(expr, "__name__", "check")
            key = (caller.f_code.co_filename, caller.f_lineno)
            name = self._full_name(f"{kind}@{caller.f_code.co_name}:{caller.f_lineno}")
        else:
            key = name = self._full_name(name)
            label = expr if isinstance(expr, str) else getattr(expr, "__name__", "check")
        fn = self._imm_fn(expr, signals, values)
        if deferred:
            root._deferred[key] = (kind, fn, name, label, msg, severity)
            if root._deferred_task is None or root._deferred_task.done():
                root._deferred_task = cocotb.start_soon(root._flush_deferred())
            return None
        return root._eval_immediate(kind, fn, name, label, msg, severity)

    def _imm_fn(self, expr, signals, values):
        if callable(expr):
            return lambda: bool(expr())
        key = (expr, tuple(sorted((signals or {}).items(), key=lambda kv: kv[0])),
               tuple(sorted((values or {}).items(), key=lambda kv: kv[0])))
        fn = self._imm_cache.get(key)
        if fn is None:
            overrides = dict(self._signals_chain())
            overrides.update(signals or {})
            dom = _Domain(None, None, "none", "immediate")
            binder = _Binder(self, dom, overrides, dict(values or {}))
            node = self._expand_lets(parse_expression(expr))
            ec = ExprCompiler(binder, expr)
            code = ec.boolean(node)
            vfn = gen_value_fn(code)

            def fn(dom=dom, vfn=vfn):
                c = [None] * len(dom.R)
                cx = [None] * len(dom.RX)
                return bool(vfn(c, dom.R, cx, dom.RX, []))
            self._imm_cache[key] = fn
        return fn

    def _eval_immediate(self, kind, fn, name, label, msg, severity):
        st = self.immediate_stats.setdefault(name, {"kind": kind, "evaluations": 0, "failures": 0, "hits": 0})
        st["evaluations"] += 1
        ok = fn()
        if kind == "cover":
            if ok:
                st["hits"] += 1
            return ok
        if not ok:
            st["failures"] += 1
            t = _now_ns(_sim_steps())
            f = Failure(name, label, t, [t], 1, severity, msg or "", immediate=True)
            self._record(f, None)
        return ok

    async def _flush_deferred(self):
        await ReadOnly()
        pending, self._deferred = self._deferred, {}
        for kind, fn, name, label, msg, severity in pending.values():
            self._eval_immediate(kind, fn, name, label, msg, severity)

    # -- reporting ------------------------------------------------------------
    def _report_failure(self, h: AssertionHandle, count, starts, at_end):
        steps_now = h.domain.now()
        t = _now_ns(steps_now)
        start_ns = [_now_ns(s[1]) for s in sorted(starts)]
        values = {}
        if not at_end:
            d = h.domain
            ticks = getattr(d, "tick_slots", ())
            for slot in sorted(h.prog.slots):
                if slot in ticks:
                    continue
                v = d.c[slot]
                if v is None:
                    try:
                        v = d.R[slot]()
                    except Exception:
                        continue
                values[d.labels[slot]] = v
        msg = h.message or ""
        if msg and "{" in msg:
            msg = self._format_message(msg, h)
        f = Failure(h.name, h.prog.text, t, start_ns, count, h.severity, msg, values, at_end)
        self._record(f, h)

    def _format_message(self, msg, h):
        import re

        def repl(m):
            try:
                handle = self._resolve_path(tuple(m.group(1).split(".")))
                return str(int(handle.value)) if handle is not None else m.group(0)
            except Exception:
                return m.group(0)
        return re.sub(r"\{([A-Za-z_][\w.]*)\}", repl, msg)

    def _record(self, f: Failure, h):
        self.failures.append(f)
        log = _log.getChild(f.name)
        level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR,
                 "fatal": logging.CRITICAL}[f.severity]
        lines = []
        if f.at_end:
            lines.append(f"FAILED at end of test: strong obligation still pending"
                         + (f" ({f.count} attempts)" if f.count > 1 else ""))
        elif f.immediate:
            lines.append("FAILED (immediate assertion)")
        else:
            starts = ", ".join(f"{s:g} ns" for s in f.start_times[:4])
            more = "" if len(f.start_times) <= 4 and f.count <= len(f.start_times) else ", ..."
            extra = f" ({f.count} attempts)" if f.count > 1 else ""
            lines.append(f"FAILED{extra}  attempt started {starts}{more}")
        lines.append(f"    {f.text}")
        if f.message:
            lines.append(f"    {f.message}")
        if f.values:
            lines.append("    sampled: " + "  ".join(f"{k}={_fmt(v)}" for k, v in f.values.items()))
        log.log(level, "\n".join(lines))
        if h is not None and h.on_fail is not None:
            h.on_fail(f)
        if f.severity == "fatal":
            raise AssertionError(str(f))

    # -- control ----------------------------------------------------------------
    def _matching(self, pattern):
        return [h for h in self.root.assertions if fnmatchcase(h.name, pattern)]

    def off(self, pattern: str = "*"):
        """Stop starting new attempts ($assertoff); attempts in flight complete."""
        for h in self._matching(self._full_pattern(pattern)):
            h.domain.wake()
            h.monitor.enabled = False

    def on(self, pattern: str = "*"):
        """Resume starting attempts ($asserton)."""
        for h in self._matching(self._full_pattern(pattern)):
            if h.initial and h.monitor.stats.attempts:
                continue            # its single attempt has started already
            h.domain.wake()
            h.monitor.enabled = True

    def kill(self, pattern: str = "*"):
        """Abort attempts in flight and stop starting new ones ($assertkill)."""
        for h in self._matching(self._full_pattern(pattern)):
            h.domain.wake()
            h.monitor.kill()
            h.monitor.enabled = False

    def _full_pattern(self, pattern):
        if self.name and not pattern.startswith(self.root.name):
            return f"{self.name}.{pattern}" if pattern != "*" else f"{self.name}.*"
        return pattern

    # -- results and finalization -------------------------------------------
    def stats(self) -> dict:
        """name -> counters (attempts, passes, vacuous, failures, ...)."""
        out = {h.name: h.stats for h in self.root.assertions}
        out.update(self.root.immediate_stats)
        return out

    @property
    def errors(self) -> list:
        return [f for f in self.root.failures if f.severity in ("error", "fatal")]

    def finalize(self, raise_on_failure: bool = True) -> list:
        """End checking: report pending strong obligations, log a summary, and
        raise AssertionError if any assertion failed with severity error/fatal."""
        root = self.root
        if root._finalized:
            return root.errors
        root._finalized = True
        if root in _test_groups:
            _test_groups.remove(root)
        for h in root.assertions:
            h.domain.wake()
            h.domain.detach(h)
            h.monitor.finish(h.domain.tickno)
        root._log_summary()
        errors = root.errors
        if errors and raise_on_failure:
            n = sum(f.count for f in errors)
            names = sorted({f.name for f in errors})
            raise AssertionError(f"{n} assertion failure(s) in {len(names)} assertion(s): "
                                 + ", ".join(names[:10]) + (" ..." if len(names) > 10 else ""))
        return errors

    def _log_summary(self):
        rows = []
        never = []
        for h in self.assertions:
            st = h.monitor.stats
            if h.kind == "cover":
                rows.append(f"  {h.name:<40} cover    hits={st.passes}")
                continue
            rows.append(f"  {h.name:<40} {h.kind:<7} attempts={st.attempts} pass={st.passes} "
                        f"vacuous={st.vacuous} fail={st.failures}" + (f" disabled={st.disabled}" if st.disabled else ""))
            if st.passes == 0 and st.failures == 0 and st.vacuous:
                never.append(h.name)
        for name, st in self.immediate_stats.items():
            if st["kind"] == "cover":
                rows.append(f"  {name:<40} cover    hits={st['hits']} of {st['evaluations']}")
            else:
                rows.append(f"  {name:<40} {st['kind']:<7} evaluations={st['evaluations']} fail={st['failures']}")
        nfail = sum(f.count for f in self.errors)
        head = f"assertion summary ({self.name}): {len(self.assertions)} concurrent, " \
               f"{len(self.immediate_stats)} immediate, {nfail} failure(s)"
        _log.info("%s\n%s", head, "\n".join(rows))
        for name in never:
            _log.warning("%s: never triggered (only vacuous successes)", name)

    def _task_ended(self):
        if not self._finalized and not self._unchecked_warned and self.errors:
            self._unchecked_warned = True
            _log.error("%d assertion failure(s) were never checked: use @checked on the test, "
                       "'async with' or call finalize()", sum(f.count for f in self.errors))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.finalize(raise_on_failure=exc_type is None)
        return False


def _fmt(v):
    return f"{v:#x}" if isinstance(v, int) and v > 9 else str(v)


def _node_text(node) -> str:
    return repr(node)


def _enum_dict(enums) -> dict:
    if isinstance(enums, dict):
        return dict(enums)
    try:
        return {m.name: int(m.value) for m in enums}
    except TypeError:
        raise TypeError("enums must be an IntEnum class or a dict") from None


def checked(func):
    """Decorator for cocotb tests: collect assertion failures and fail the test at its end.

        @cocotb.test()
        @checked
        async def test_traffic(dut): ...
    """
    if not inspect.iscoroutinefunction(func):
        raise TypeError("@checked must decorate an async test function")

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = len(_test_groups)
        try:
            await func(*args, **kwargs)
        except BaseException:
            for g in _test_groups[start:]:
                g.finalize(raise_on_failure=False)
            del _test_groups[start:]
            raise
        groups = _test_groups[start:]
        del _test_groups[start:]
        errors = []
        for g in groups:
            errors += g.finalize(raise_on_failure=False)
        if errors:
            n = sum(f.count for f in errors)
            names = sorted({f.name for f in errors})
            raise AssertionError(f"{n} assertion failure(s) in {len(names)} assertion(s): "
                                 + ", ".join(names[:10]) + (" ..." if len(names) > 10 else ""))
    return wrapper


class Checker:
    """Reusable bundle of assertions with ports, like an SV checker bound with `bind`.

        class StreamChecker(Checker):
            ports = ("clk", "rst_n", "valid", "ready", "data")
            clock = "posedge clk"
            disable_iff = "!rst_n"

            def build(self, a):
                a.assert_property("valid && !ready |=> valid && $stable(data)", name="valid_held")

        StreamChecker(a, name="in", clk=dut.clk, rst_n=dut.rst_n, valid=dut.in_valid, ...)
        StreamChecker(a, name="lane0", scope=dut.g_lane[0])   # ports found by name in scope

    A port can be bound to a handle or to an expression string.
    """
    ports: tuple = ()
    clock = None
    disable_iff = None

    def __init__(self, group: Assertions, name: str | None = None, scope=None, **bindings):
        unknown = set(bindings) - set(self.ports)
        if unknown:
            raise TypeError(f"{type(self).__name__} has no port(s) {', '.join(sorted(unknown))}")
        signals, lets = {}, {}
        for p in self.ports:
            if p in bindings:
                v = bindings[p]
                if isinstance(v, str):
                    lets[p] = v
                else:
                    signals[p] = v
        name = name or type(self).__name__
        full = f"{group.name}.{name}" if group.name else name
        self.assertions = Assertions(scope=scope if scope is not None else group.scope,
                                     clock=self.clock, disable_iff=self.disable_iff, name=full,
                                     signals=signals, lets=lets, parent=group)
        self.build(self.assertions)

    def build(self, a: Assertions):
        raise NotImplementedError("Checker subclasses implement build(self, a)")
