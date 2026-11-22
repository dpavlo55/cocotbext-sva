"""Offline checking: concurrent assertions evaluated on a waveform dump (VCD,
or FST with the compiled kernel installed) after the simulation, with no
simulation overhead.

Signals are sampled at each clock edge of the dump with the value they had
just before the edge's time step (a change in the same time step as the edge
counts as after it): the exact SVA preponed semantics, which online sampling
in a clock callback approximates.
"""
from __future__ import annotations

import re
from array import array
from dataclasses import dataclass, field

from ._errors import SvaError, SvaUnsupportedError
from ._prop import FAIL
from ._trace import TraceRun

_UNITS = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}


class _Dump:
    """Variables of a waveform dump, by full dotted name."""

    def __init__(self, path):
        self.path = path
        self.ns_per_unit = 1.0
        # full name -> (id code, width); a vector dumped one bit per variable
        # ("$var wire 1 ( tag [3] $end") has a tuple ((id code, shift), ...)
        # in place of the id code
        self.vars: dict = {}
        self._bits: dict = {}          # full name -> [id code of each bit, leftmost first]
        self._lower: dict = {}         # lower-case full name -> full name

    def _add_var(self, full, width, code):
        """full: dotted name, possibly with a range or bit select
        ("tb.data [7:0]", "tb.data[7:0]", "tb.tag [3]")."""
        m = re.fullmatch(r"(.*?)\s*\[(-?\d+)\]", full)
        if m and width == 1:                       # one bit of a vector
            self._bits.setdefault(m.group(1), []).append(code)
            return
        full = re.sub(r"\s*\[[^.]*\]$", "", full)   # "data [7:0]" -> "data"
        self.vars.setdefault(full, (code, width))
        self._lower.setdefault(full.lower(), full)

    def _end_vars(self):
        for full, codes in self._bits.items():
            if full not in self.vars:
                n = len(codes)
                self.vars[full] = (tuple((c, n - 1 - k) for k, c in enumerate(codes)), n)
                self._lower.setdefault(full.lower(), full)

    def find(self, name: str):
        """Full name -> (id code, width), exact or case-insensitive; or None."""
        hit = self.vars.get(name)
        if hit is None:
            full = self._lower.get(name.lower())
            hit = self.vars.get(full) if full is not None else None
        return hit


class VcdReader(_Dump):
    """Header of a VCD file plus a streaming iterator over its time steps."""

    def __init__(self, path):
        super().__init__(path)
        self._f = open(path, "r", encoding="utf-8", errors="replace")
        self._tokens = self._token_stream()
        self._read_header()

    def close(self):
        self._f.close()

    def _token_stream(self):
        for line in self._f:
            yield from line.split()

    def _read_header(self):
        scope = []
        toks = self._tokens
        for tok in toks:
            if tok == "$timescale":
                spec = []
                for t in toks:
                    if t == "$end":
                        break
                    spec.append(t)
                m = re.fullmatch(r"(\d+)\s*([a-z]+)", "".join(spec))
                if not m or m.group(2) not in _UNITS:
                    raise SvaError(f"{self.path}: unsupported timescale {' '.join(spec)!r}")
                self.ns_per_unit = int(m.group(1)) * _UNITS[m.group(2)]
            elif tok == "$scope":
                _kind, name = next(toks), next(toks)
                scope.append(name)
                self._skip_to_end()
            elif tok == "$upscope":
                scope.pop()
                self._skip_to_end()
            elif tok == "$var":
                body = []
                for t in toks:
                    if t == "$end":
                        break
                    body.append(t)
                _kind, width, code = body[0], int(body[1]), body[2]
                self._add_var(".".join(scope + ["".join(body[3:])]), width, code)
            elif tok == "$enddefinitions":
                self._skip_to_end()
                self._end_vars()
                return
            elif tok.startswith("$"):
                self._skip_to_end()
        raise SvaError(f"{self.path}: no $enddefinitions (not a VCD file?)")

    def _skip_to_end(self):
        for t in self._tokens:
            if t == "$end":
                return

    def stream(self, wanted: set, on_step):
        """on_step(time, [(id code, value string), ...]) per time step."""
        for time, changes in self.steps(wanted):
            on_step(time, changes)

    def steps(self, wanted: set):
        """Yield (time, [(id code, value string), ...]) per time step, only
        for the wanted id codes. Value strings are "0", "1", "x", "z" or a
        binary vector."""
        time, changes = 0, []
        toks = self._tokens
        for tok in toks:
            c = tok[0]
            if c == "#":
                if changes:
                    yield time, changes
                    changes = []
                time = int(tok[1:])
            elif c in "01xzXZ":
                code = tok[1:]
                if code in wanted:
                    changes.append((code, c.lower()))
            elif c in "bB":
                code = next(toks)
                if code in wanted:
                    changes.append((code, tok[1:].lower()))
            elif c in "rR":
                code = next(toks)
                if code in wanted:
                    raise SvaError(f"{self.path}: real-valued signals are not supported")
            elif c == "$":
                if tok in ("$comment",):
                    self._skip_to_end()
                # $dumpvars / $dumpall / $dumpon / $dumpoff / $end: value lists follow or end
        if changes:
            yield time, changes


class FstReader(_Dump):
    """An FST file, read by the compiled kernel (cocotbext-sva-kernel)."""

    def __init__(self, path):
        super().__init__(path)
        try:
            import cocotbext_sva_kernel as kernel
        except ImportError:
            raise SvaError(f"{path}: reading FST needs the compiled kernel "
                           "(pip install cocotbext-sva[fast]); or dump VCD") from None
        if not hasattr(kernel, "FstFile"):
            raise SvaError(f"{path}: the installed cocotbext-sva-kernel cannot read FST; update it")
        self._fst = kernel.FstFile(str(path))
        self.ns_per_unit = 10.0 ** (self._fst.timescale_exponent + 9)
        self._real = set()
        for full, handle, width, real in self._fst.vars():
            self._add_var(full, width, handle)
            if real:
                self._real.add(handle)
        self._end_vars()

    def close(self):
        self._fst = None

    def stream(self, wanted: set, on_step):
        """on_step(time, [(handle, value string), ...]) per time step."""
        if wanted & self._real:
            raise SvaError(f"{self.path}: real-valued signals are not supported")
        self._fst.read_all(sorted(wanted), on_step)


def open_dump(path):
    """A VcdReader or FstReader, by the file's content: a VCD file is text
    starting with a $ keyword; FST is binary (a header block, or a gzip
    wrapper as nvc and GHDL write)."""
    with open(path, "rb") as f:
        head = f.read(256).lstrip()
    return VcdReader(path) if head.startswith(b"$") else FstReader(path)


def _value(s: str, width: int):
    """VCD value string -> (value, X mask)."""
    if len(s) < width:                 # left-extend: x/z with itself, 0/1 with 0
        s = (s[0] if s[0] in "xz" else "0") * (width - len(s)) + s
    if "x" not in s and "z" not in s:
        return int(s, 2), 0
    v = int(s.replace("x", "0").replace("z", "0"), 2)
    m = int("".join("1" if ch in "xz" else "0" for ch in s), 2)
    return v, m


@dataclass
class OfflineFailure:
    assertion: str
    time_ns: float                 # time of the clock edge where the attempt failed
    start_times_ns: list           # start times of the attempts (oldest first)
    count: int
    at_end: bool = False           # pending strong obligation at the end of the dump

    def __str__(self):
        when = "end of dump" if self.at_end else f"{self.time_ns:g} ns"
        starts = ", ".join(f"{t:g} ns" for t in self.start_times_ns[:4])
        more = f" (+{self.count - 4} more)" if self.count > 4 else ""
        return f"{self.assertion} FAILED at {when}, attempt started {starts}{more}"


@dataclass
class OfflineResult:
    failures: list = field(default_factory=list)       # [OfflineFailure]
    stats: dict = field(default_factory=dict)          # name -> counters
    cycles: int = 0
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        lines = [f"{self.cycles} clock cycles checked, {len(self.failures)} failure report(s)"]
        for name, st in self.stats.items():
            if "hits" in st:
                lines.append(f"  {name}: cover, {st['hits']} hit(s) in {st['attempts']} attempts")
            else:
                lines.append(f"  {name}: {st['attempts']} attempts, {st['passes']} passed, "
                             f"{st['vacuous']} vacuous, {st['failures']} failed")
        lines += [f"  {f}" for f in self.failures]
        return "\n".join(lines)

    def check(self):
        """Raise AssertionError listing the failures, if any."""
        if self.failures:
            raise AssertionError("assertion failures in the dump:\n" + "\n".join(map(str, self.failures)))


class _Sink:
    wants_pass = False
    wants_vacuous = False

    def __init__(self, checker, group):
        self.checker = checker
        self.group = group

    def __call__(self, verdict, mon, count, starts, tickno, at_end=False):
        if verdict == FAIL:
            self.checker._failure(self.group, mon.name, count, starts, tickno, at_end)


class _Group:
    """Assertions evaluated on one set of clocks: one clock (ticks: None), or
    the clocks of multi-clock assertions (a tick at every time step in which
    one of them ticks; ticks[i] names the row signal telling whether clock i
    does)."""

    def __init__(self, clocks, ticks):
        self.clocks = clocks
        self.ticks = ticks
        self.props: dict = {}
        self.kinds: dict = {}
        self.specs: dict = {}
        self.run = None
        self.times = array("q")


class TraceSampler:
    """Concurrent assertions checked on a waveform dump after the simulation:
    VCD, or FST when the compiled kernel (cocotbext-sva[fast]) is installed
    (the format is detected from the file's content).

        ts = TraceSampler("waves.vcd", clock="posedge clk", scope="tb.dut",
                          disable_iff="!rst_n")
        ts.assert_property("req |-> ##[1:3] ack", name="req_ack")
        ts.cover_property("req ##1 req")
        ts.assert_property("req |=> @(posedge clk_b) ##[1:4] ack_b")   # multi-clock
        result = ts.run()          # result.ok, result.failures, result.summary()

    Names in properties are resolved under `scope` (dot-separated, as in the
    dump; case-insensitive fallback). `clock` is the default clock; a
    property may name its own clocks (`@(negedge clk) ...`, `@(posedge c iff
    en)`), several of them too (multi-clock properties: as online, one tick
    per time step in which one of its clocks ticks). Immediate assertions,
    local-variable-free and local-variable properties, sampled-value
    functions and $isunknown (x/z in the dump) are supported; real-valued
    signals are not.
    """

    def __init__(self, path, clock: str, *, scope: str = "", disable_iff: str | None = None,
                 params: dict | None = None):
        m = re.fullmatch(r"\s*(posedge|negedge|edge)?\s*(\S+)\s*", clock)
        if not m:
            raise SvaError(f"clock must be '[posedge|negedge|edge] <signal>', got {clock!r}")
        self.edge = m.group(1) or "posedge"
        self.clock_name = m.group(2)
        self.path = path
        self.scope = scope
        self.disable_iff = disable_iff
        self.params = dict(params or {})
        self.props: dict = {}
        self.kinds: dict = {}

    # -- assertions ---------------------------------------------------------
    def _add(self, kind, prop, name):
        name = name or f"{kind}{len(self.props) + 1}"
        if name in self.props:
            raise SvaError(f"duplicate assertion name {name!r}")
        text = str(getattr(prop, "text", prop))
        from ._parser import parse_assertion
        parse_assertion(text)                       # syntax errors now
        if self.disable_iff and kind != "cover" and "disable iff" not in text:
            text = f"disable iff ({self.disable_iff}) {text}"
        self.props[name] = text
        self.kinds[name] = kind

    def assert_property(self, prop, name: str | None = None):
        self._add("assert", prop, name)

    def assume_property(self, prop, name: str | None = None):
        self._add("assume", prop, name)

    def cover_property(self, prop, name: str | None = None):
        self._add("cover", prop, name)

    # -- evaluation ---------------------------------------------------------
    def _full(self, name):
        return f"{self.scope}.{name}" if self.scope else name

    def run(self, until_ns: float | None = None) -> OfflineResult:
        """Check the dump; with until_ns, only its time steps up to that time
        (e.g. the end of the test, when the simulator dumped beyond it)."""
        dump = open_dump(self.path)
        try:
            return self._run(dump, until_ns)
        finally:
            dump.close()

    def _run(self, vcd: _Dump, until_ns=None) -> OfflineResult:
        from . import _ast as A
        from ._compile import _Ctx, gen_value_fn
        from ._expr import ExprCompiler
        from ._multiclock import Rewriter, strip_clocks, uses_clocks
        from ._parser import parse_assertion
        from ._trace import TraceBinder

        resolved = {}

        def find(name):
            return vcd.find(self._full(name)) or vcd.find(name)

        def resolver(name):
            hit = find(name)
            if hit is None:
                return None
            resolved[name] = hit
            return hit[1]

        def names():
            pre = self.scope + "." if self.scope else ""
            return [n[len(pre):] for n in vcd.vars if n.startswith(pre)]

        # clocks: key (id code, edge, iff text) -> (id code, edge, iff node)
        clocks: dict = {}

        def clock_key(name, edge, iff=None):
            hit = find(name)
            if hit is None:
                raise SvaError(f"clock {name!r} not found in {self.path} (scope {self.scope!r})")
            if type(hit[0]) is tuple:
                raise SvaError(f"clock {name!r} is a vector in {self.path}")
            key = (hit[0], edge, None if iff is None else repr(iff))
            clocks.setdefault(key, (hit[0], edge, iff))
            return key

        def event_key(ev):
            if not isinstance(ev.signal, A.Id):
                raise SvaError("a clock must be a signal name")
            return clock_key(ev.signal.name, ev.edge or "edge", ev.iff)

        default = clock_key(self.clock_name, self.edge)

        # group the assertions by the clocks they use
        groups: dict = {}
        for name, text in self.props.items():
            spec = parse_assertion(text)
            kind = self.kinds[name]
            first = event_key(spec.clock) if spec.clock is not None else default
            if uses_clocks(spec.prop, {}):
                const = ExprCompiler(TraceBinder({}, self.params, resolver, names), text).const
                _, used = Rewriter(event_key, None, const, {}, kind, text).analyse(spec.prop, first)
                if len(used) == 1:                  # every clocking event names one clock
                    spec = A.AssertionSpec(None, spec.disable, strip_clocks(spec.prop), span=spec.span)
                    gkey = tuple(used)
                else:
                    gkey = tuple(sorted(used, key=repr))
            else:
                gkey = (first,)
            g = groups.get(gkey)
            if g is None:
                k = len(groups)
                g = groups[gkey] = _Group(list(gkey), None if len(gkey) == 1
                                          else [f"$tick{k}_{i}" for i in range(len(gkey))])
            g.props[name], g.kinds[name], g.specs[name] = text, kind, spec

        self.result = OfflineResult()
        self._unit = vcd.ns_per_unit
        tick_names = set()
        vals, xset = [], set()
        for g in groups.values():
            clock_id = clock = None
            if g.ticks is not None:
                tick_of = dict(zip(g.clocks, g.ticks))
                tick_names |= set(g.ticks)

                def clock_id(ev, _t=tick_of):
                    return _t[event_key(ev)]
                clock = tick_of.get(default)
            g.run = TraceRun(g.props, {}, self.params, kinds=g.kinds, initial="first",
                             sink=_Sink(self, g), resolver=resolver, names=names, clock=clock,
                             specs=g.specs, clock_id=clock_id)
            self.result.warnings += g.run.result.warnings
            v, x = g.run.signal_names
            vals += v
            xset |= set(x)

        # iff conditions of clocking events, on the sampled values
        cur = {}                                    # row of current (sampled) values
        iff_fns = {}
        for key, (_, _, iff) in clocks.items():
            if iff is None:
                continue
            b = TraceBinder({}, self.params, resolver, names)
            fn = gen_value_fn(ExprCompiler(_Ctx(b, key[2], {}, set()), key[2]).boolean(iff))
            R = [(lambda n=n: cur.get(n, 0)) for n in b.slot_names]
            RX = [(lambda n=n: cur.get((n, "x"), 0)) for n in b.xslot_names]
            iff_fns[key] = (fn, R, RX, len(b.slot_names), len(b.xslot_names), len(b.channels))
            vals += b.slot_names
            xset |= set(b.xslot_names)

        by_code: dict = {}                          # id code -> [(name, width, bit or None)]
        for name in dict.fromkeys(vals):
            if name in tick_names:
                continue
            code, width = resolved[name]
            if type(code) is tuple:                 # vector dumped bit by bit
                for c, bit in code:
                    by_code.setdefault(c, []).append((name, width, bit))
            else:
                by_code.setdefault(code, []).append((name, width, None))
            cur[name] = 0
            if name in xset:
                cur[(name, "x")] = 0
        clk_val = dict.fromkeys(c for c, _, _ in clocks.values())   # None: no value yet
        wanted = set(by_code) | set(clk_val)
        plan = [(key, code, edge, iff_fns.get(key)) for key, (code, edge, _) in clocks.items()]
        glist = [(g.run.step, g.times, g.clocks[0] if g.ticks is None else None,
                  None if g.ticks is None else list(zip(g.clocks, g.ticks)))
                 for g in groups.values()]
        cycles = [0]
        last = None if until_ns is None else until_ns / vcd.ns_per_unit + 0.5   # (dump time units)

        def on_step(time, changes):
            if last is not None and time > last:
                return
            edges = None
            for code, v in changes:
                if code in clk_val:
                    new = v[-1] if len(v) > 1 else v
                    old = clk_val[code]
                    clk_val[code] = new
                    if old is not None and new != old:    # (the first value is not an edge)
                        rose = (old == "0" and new != "0") or (old in "xz" and new == "1")
                        fell = (old == "1" and new != "1") or (old in "xz" and new == "0")
                        if edges is None:
                            edges = {}
                        edges[code] = (rose, fell)
            if edges is not None:
                ticked = set()
                for key, code, edge, iff in plan:
                    e = edges.get(code)
                    if e is None:
                        continue
                    if e[0] if edge == "posedge" else e[1] if edge == "negedge" else e[0] or e[1]:
                        if iff is not None:
                            fn, R, RX, n, nx, nh = iff
                            if not fn([None] * n, R, [None] * nx, RX, [None] * nh):
                                continue
                        ticked.add(key)
                stepped = False
                for step, times, single, ticks in glist:
                    if single is not None:
                        if single not in ticked:
                            continue
                    else:
                        hit = False
                        for key, tn in ticks:
                            if key in ticked:
                                cur[tn] = 1
                                hit = True
                            else:
                                cur[tn] = 0
                        if not hit:
                            continue
                    times.append(time)
                    step(cur)                       # values before this time step
                    stepped = True
                if stepped:
                    cycles[0] += 1
            for code, v in changes:
                for name, width, bit in by_code.get(code, ()):
                    if bit is None:
                        val, xm = _value(v, width)
                    else:                           # one bit of the vector
                        b, xb = _value(v, 1)
                        m = 1 << bit
                        val = (cur[name] & ~m) | (b << bit)
                        xm = (cur.get((name, "x"), 0) & ~m) | (xb << bit)
                    cur[name] = val
                    if name in xset:
                        cur[(name, "x")] = xm

        vcd.stream(wanted, on_step)
        stats = {}
        for g in groups.values():
            stats.update(g.run.finish().stats)
        self.result.cycles = cycles[0]
        for name in self.props:
            st = stats[name]
            if self.kinds[name] == "cover":
                st = {"attempts": st["attempts"], "hits": st["passes"]}
            self.result.stats[name] = st
        return self.result

    def _failure(self, group, name, count, starts, tickno, at_end):
        times, unit = group.times, self._unit

        def at(tick):
            return times[min(tick, len(times) - 1)] * unit if times else 0.0
        self.result.failures.append(OfflineFailure(
            name, at(tickno), [at(s[0]) for s in sorted(starts)], count, at_end))
