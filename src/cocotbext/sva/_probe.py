"""ProbeSampler: sampling done by a small generated HDL module.

A `Probe` describes the signals a group of assertions reads and the clock
that samples them. `Probe.write()` generates a module (SystemVerilog) or an
entity (VHDL-2008) that, at every tick of the clock, stores the signals in
one packed vector of a buffer of `depth` samples, with the time of the tick,
and toggles `flush` when the buffer is full. In the test, the assertions of
`Assertions(dut, clock=probe.attach(dut))` are evaluated on those samples:

* the values are taken inside the simulator at the clock edge, as a
  flip-flop on that clock would take them;
* Python reads one vector per wake-up instead of one value per signal;
* with depth > 1 Python wakes only once every `depth` ticks. Failures are
  then reported up to `depth` ticks late, with their exact time.

This module has no cocotb dependency (it also runs in build scripts); the
part that runs in the simulator is `_ProbeDomain` in `_api.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ._errors import SvaError

KINDS = ("logic", "unsigned", "signed", "integer", "boolean", "enum")
STAMP_BITS = 64
COUNT_BITS = 32


@dataclass(frozen=True)
class ProbeSignal:
    name: str          # as used in assertions, relative to the probe's scope ("u_ctrl.count")
    width: int
    kind: str          # KINDS
    vhdl_type: str     # type mark of the VHDL signal
    offset: int        # bit offset in a sample

    @property
    def signed(self) -> bool:
        return self.kind in ("signed", "integer")


class Probe:
    """Signals sampled by a generated HDL probe.

    Args:
        name: identifies the probe; the module / entity is `sva_probe_<name>`.
        clock: "posedge clk", "negedge clk" or "edge clk" (a path relative to
            `scope`); an `iff` is given to attach().
        signals: name -> width, or (width, kind). Names are paths relative to
            `scope`. kind is "logic" (the default: std_logic, std_logic_vector,
            any Verilog vector), "unsigned", "signed", "integer", "boolean",
            or "enum <vhdl type name>" (sampled as its position number).
            Verilog only needs "signed" where it matters; the others tell the
            VHDL generator which type the signal has.
        top: name of the top-level module / entity of the simulation.
        scope: instance path below `top` in which the names are looked up.
        depth: number of samples buffered in the HDL (1: Python is woken at
            every tick).
    """

    def __init__(self, name: str, clock: str, signals: dict, *, top: str = "dut", scope: str = "",
                 depth: int = 1):
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise SvaError(f"probe name {name!r} must be an identifier")
        m = re.fullmatch(r"\s*(posedge|negedge|edge)?\s*([\w.]+)\s*", clock)
        if not m:
            raise SvaError(f"clock must be '[posedge|negedge|edge] <signal>', got {clock!r}")
        if depth < 1:
            raise SvaError("depth must be at least 1")
        if not signals:
            raise SvaError("a probe needs at least one signal")
        self.name = name
        self.module = f"sva_probe_{name}"
        self.edge = m.group(1) or "posedge"
        self.clock = m.group(2)
        self.top = top
        self.scope = scope
        self.depth = depth
        self.signals: dict[str, ProbeSignal] = {}
        off = 0
        for sig, spec in signals.items():
            width, kind = (spec, "logic") if isinstance(spec, int) else spec
            vtype = ""
            if kind.startswith("enum"):
                vtype = kind[4:].strip()
                kind = "enum"
                if not vtype:
                    raise SvaError(f"{sig}: give the VHDL type name: 'enum state_t'")
            if kind not in KINDS:
                raise SvaError(f"{sig}: unknown kind {kind!r} (one of {', '.join(KINDS)})")
            if width < 1:
                raise SvaError(f"{sig}: width must be at least 1")
            if kind == "boolean" and width != 1:
                raise SvaError(f"{sig}: a boolean is 1 bit wide")
            self.signals[sig] = ProbeSignal(sig, width, kind, vtype, off)
            off += width
        self.width = off

    # -- generation -------------------------------------------------------------
    def write(self, path, language: str | None = None, bind: bool = True,
              interface: str = "names") -> Path:
        """Write the probe to `path` (.sv / .v: SystemVerilog, .vhd / .vhdl: VHDL-2008).

        SystemVerilog. The module refers to the signals by hierarchical names
        (`top.scope.signal`). With bind=True the file ends with a `bind` of the
        probe into the top-level module. With bind=False the probe is a
        top-level module of its own, to be elaborated beside the design (for
        simulators without `bind`, or that do not elaborate a `bind` at file
        level).

        VHDL. interface="names": the entity has no ports and refers to the
        signals by external names; instantiate it after the design.
        interface="ports": the entity has one input port per signal (named
        like it, dots replaced by underscores) and `sva_clk`, to be mapped where
        it is instantiated (for simulators without external names)."""
        path = Path(path)
        if language is None:
            language = "vhdl" if path.suffix.lower() in (".vhd", ".vhdl") else "verilog"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.hdl(language, bind=bind, interface=interface), encoding="utf-8",
                        newline="\n")
        return path

    def hdl(self, language: str, bind: bool = True, interface: str = "names") -> str:
        if interface not in ("names", "ports"):
            raise SvaError("interface must be 'names' or 'ports'")
        if language == "vhdl":
            return self._vhdl(interface == "ports")
        if language == "verilog":
            return self._sv(bind)
        raise SvaError("language must be 'verilog' or 'vhdl'")

    def _path(self, name: str, sep: str = ".") -> str:
        parts = [self.top] + ([self.scope] if self.scope else []) + [name]
        return sep.join(parts)

    def _sv(self, bind: bool) -> str:
        fields = []
        for s in reversed(list(self.signals.values())):       # the first signal is the low bits
            fields.append(f"{s.width}'({self._path(s.name)})")
        event = {"posedge": "posedge ", "negedge": "negedge ", "edge": ""}[self.edge] + self._path(self.clock)
        sb = STAMP_BITS
        lines = [
            "// Generated by cocotbext.sva (Probe) -- do not edit.",
            f"// Samples {len(self.signals)} signal(s) of {self._path('')[:-1]} at {self.edge} {self.clock}.",
            f"module {self.module};",
            f"  localparam int SVA_W = {self.width};",
            f"  localparam int SVA_DEPTH = {self.depth};",
            "  logic [SVA_DEPTH*SVA_W-1:0] sva_data /* verilator public_flat_rd */ = '0;",
            f"  logic [SVA_DEPTH*{sb}-1:0] sva_stamp /* verilator public_flat_rd */ = '0;",
            f"  logic [{COUNT_BITS - 1}:0] sva_count /* verilator public_flat_rd */ = '0;",
            "  logic sva_flush /* verilator public_flat_rd */ = 1'b0;",
            "  wire [SVA_W-1:0] sva_sample = {" + ", ".join(fields) + "};",
            f"  always @({event}) begin",
            "    sva_data[(sva_count % SVA_DEPTH) * SVA_W +: SVA_W] <= sva_sample;",
            f"    sva_stamp[(sva_count % SVA_DEPTH) * {sb} +: {sb}] <= $time;",
            "    sva_count <= sva_count + 1;",
            "    if (sva_count % SVA_DEPTH == SVA_DEPTH - 1) sva_flush <= ~sva_flush;",
            "  end",
            "endmodule",
        ]
        if bind:
            lines += ["", f"bind {self.top} {self.module} {self.module} ();"]
        return "\n".join(lines) + "\n"

    def _vhdl(self, ports: bool = False) -> str:
        def ext(name, vtype):
            return f"<< signal .{self._path(name)} : {vtype} >>"

        aliases, fields, port_decls = [], [], ["sva_clk : in std_logic"]
        for i, s in enumerate(self.signals.values()):
            ref = s.name.replace(".", "_") if ports else f"sva_s{i}"
            rng = f"({s.width - 1} downto 0)"
            if s.kind == "logic":
                vtype = "std_logic" if s.width == 1 else "std_logic_vector"
                ptype = vtype if s.width == 1 else vtype + rng
                conv = ref
            elif s.kind in ("unsigned", "signed"):
                vtype, ptype, conv = s.kind, s.kind + rng, f"std_logic_vector({ref})"
            elif s.kind == "integer":
                vtype = ptype = "integer"
                conv = f"std_logic_vector(to_signed({ref}, {s.width}))"
            elif s.kind == "boolean":
                vtype = ptype = "boolean"
                conv = f"sva_b2l({ref})"
            else:
                vtype = ptype = s.vhdl_type
                conv = f"std_logic_vector(to_unsigned({s.vhdl_type}'pos({ref}), {s.width}))"
            aliases.append(f"  alias sva_s{i} is {ext(s.name, vtype)};")
            port_decls.append(f"{ref} : in {ptype}")
            hi, lo = s.offset + s.width - 1, s.offset
            if s.kind == "logic" and s.width == 1 or s.kind == "boolean":
                fields.append(f"      sva_v({lo}) := {conv};")
            else:
                fields.append(f"      sva_v({hi} downto {lo}) := {conv};")
        cond = {"posedge": "rising_edge(sva_clk)", "negedge": "falling_edge(sva_clk)",
                "edge": "sva_clk'event"}[self.edge]
        sb = STAMP_BITS
        if ports:
            head = ["-- VHDL-2008. Instantiate it with its ports mapped to the signals:",
                    f"--     {self.module}: entity work.{self.module} port map (sva_clk => ..., ...);"]
            port_lines = ["  port (", *[f"    {p};" for p in port_decls[:-1]], f"    {port_decls[-1]}", "  );"]
            alias_lines = []
        else:
            head = ["-- VHDL-2008 (external names). Instantiate it after the design it samples:",
                    f"--     {self.module}: entity work.{self.module};"]
            port_lines = []
            alias_lines = [f"  alias sva_clk is {ext(self.clock, 'std_logic')};", *aliases]
        lines = [
            "-- Generated by cocotbext.sva (Probe) -- do not edit.",
            f"-- Samples {len(self.signals)} signal(s) of .{self._path('')[:-1]} at {self.edge} {self.clock}.",
            *head,
            "library ieee;",
            "use ieee.std_logic_1164.all;",
            "use ieee.numeric_std.all;",
            "use std.env.all;",
            "",
            f"entity {self.module} is",
            *port_lines,
            "end entity;",
            "",
            f"architecture gen of {self.module} is",
            f"  constant SVA_W : positive := {self.width};",
            f"  constant SVA_DEPTH : positive := {self.depth};",
            "  signal sva_data  : std_logic_vector(SVA_DEPTH * SVA_W - 1 downto 0) := (others => '0');",
            f"  signal sva_stamp : std_logic_vector(SVA_DEPTH * {sb} - 1 downto 0) := (others => '0');",
            f"  signal sva_count : unsigned({COUNT_BITS - 1} downto 0) := (others => '0');",
            "  signal sva_flush : std_logic := '0';",
            *alias_lines,
            "  function sva_b2l(b : boolean) return std_logic is",
            "  begin",
            "    if b then return '1'; else return '0'; end if;",
            "  end function;",
            "begin",
            "  process (sva_clk)",
            "    variable sva_v : std_logic_vector(SVA_W - 1 downto 0);",
            "    variable sva_idx : natural;",
            "    variable sva_hi, sva_lo : natural;",
            "    constant sva_giga : time := resolution_limit * 1000000000;",
            "  begin",
            f"    if {cond} then",
            *fields,
            "      sva_idx := to_integer(sva_count mod SVA_DEPTH);",
            "      sva_data((sva_idx + 1) * SVA_W - 1 downto sva_idx * SVA_W) <= sva_v;",
            "      -- the time in units of the simulator's resolution, as two integers",
            "      sva_hi := now / sva_giga;",
            "      sva_lo := (now - sva_hi * sva_giga) / resolution_limit;",
            f"      sva_stamp((sva_idx + 1) * {sb} - 1 downto sva_idx * {sb}) <= std_logic_vector(",
            f"        resize(to_unsigned(sva_hi, 32) * to_unsigned(1000000000, 32), {sb}) + sva_lo);",
            "      sva_count <= sva_count + 1;",
            "      if sva_idx = SVA_DEPTH - 1 then",
            "        sva_flush <= not sva_flush;",
            "      end if;",
            "    end if;",
            "  end process;",
            "end architecture;",
        ]
        return "\n".join(lines) + "\n"

    # -- in the simulation ------------------------------------------------------
    def attach(self, dut, instance=None, iff: str | None = None):
        """Clocking event for `Assertions(dut, clock=probe.attach(dut))`.

        instance: handle of the probe instance; by default `dut.sva_probe_<name>`
        (where the generated `bind` puts it) or a top-level module of that name."""
        from ._api import ProbeClock
        return ProbeClock(self, dut, instance, iff)
