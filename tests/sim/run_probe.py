"""Assertions on the samples of generated HDL probes (Probe) against the same
assertions sampled from Python, on the simulator given by SIM."""
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parents[1]))
sys.path.insert(0, str(here.parents[1] / "src"))
sys.path.insert(0, str(here))
from devtools.simrun import run, selected_lang, selected_sim  # noqa: E402

sim = selected_sim()
lang = selected_lang(sim, [here / "dut.vhd"], [here / "dut.sv"])
os.environ["TOPLEVEL_LANG"] = lang
import probes  # noqa: E402

gen = here / "sim_build" / f"probe_gen_{sim}_{lang}"
made = probes.make(lang == "vhdl")
test = os.environ.get("PROBE_TEST", "test_probe")
if lang == "vhdl":
    # GHDL (with cocotb) does not start with external names in the design: ports
    ports = sim == "ghdl"
    files = [p.write(gen / f"{p.module}.vhd", interface="ports" if ports else "names")
             for p in made.values()]
    run(here=here, toplevel="dut_probe", test_module=test, tag="probe",
        vhdl=[here / "dut.vhd"] + files + [here / ("dut_probe_ports.vhd" if ports else "dut_probe.vhd")],
        verilog=[])
else:
    # Verilator elaborates one top-level module: the probes are bound into the
    # DUT. Icarus has no bind, and the commercial simulator does not elaborate a
    # bind at file level: there the probes are top-level modules beside the DUT.
    bind = sim == "verilator"
    files = [p.write(gen / f"{p.module}.sv", bind=bind) for p in made.values()]
    roots = [] if bind else [p.module for p in made.values()]
    run(here=here, toplevel="dut", test_module=test, tag="probe",
        vhdl=[], verilog=[here / "dut.sv"] + files,
        extra_build_args=[x for r in roots for x in ("-s", r)] if sim == "icarus" else [],
        extra_test_args=[f"top.{r}" for r in roots] if sim not in ("icarus", "verilator") else [])
