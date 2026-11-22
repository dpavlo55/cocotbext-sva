"""Benchmark of the generated HDL probe (Probe) on the simulator given by SIM:
the same assertions sampled from Python at the clock edge, through a probe
that hands over every sample, and through one that buffers 64."""
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
sys.path.insert(0, str(here.parent / "src"))
from devtools.simrun import run, selected_lang, selected_sim  # noqa: E402

from cocotbext.sva import Probe  # noqa: E402

sim = selected_sim()
lang = selected_lang(sim, [here / "bench.vhd"], [here / "bench.sv"])
if sim == "ghdl":
    print("probe benchmark not run on GHDL: its probes use external names")
    sys.exit(0)
vhdl = lang == "vhdl"
top = dict(top="bench_probe", scope="u") if vhdl else dict(top="bench")
SIGNALS = {"req": 1, "ack": 1, "rare": 1, "s8": 8}
gen = here / "sim_build" / f"probe_gen_{sim}_{lang}"
out = here / os.environ.get("BENCH_RESULTS", "results_probe.jsonl")
env = {"BENCH_OUT": str(out), "BENCH_CYCLES": os.environ.get("BENCH_CYCLES", "20000"),
       "TOPLEVEL_LANG": lang}
probes = [Probe(f"b{d}", "posedge clk", SIGNALS, depth=d, **top) for d in (1, 64)]
if vhdl:
    files = [p.write(gen / f"{p.module}.vhd") for p in probes]
    run(here=here, toplevel="bench_probe", test_module=os.environ.get("PROBE_TEST", "test_probe_bench"), tag="probe",
        vhdl=[here / "bench.vhd"] + files + [here / "bench_probe.vhd"], verilog=[], extra_env=env)
else:
    bind = sim == "verilator"
    files = [p.write(gen / f"{p.module}.sv", bind=bind) for p in probes]
    roots = [] if bind else [p.module for p in probes]
    run(here=here, toplevel="bench", test_module=os.environ.get("PROBE_TEST", "test_probe_bench"), tag="probe",
        vhdl=[], verilog=[here / "bench.sv"] + files, public=False, extra_env=env,
        extra_build_args=[x for r in roots for x in ("-s", r)] if sim == "icarus" else [],
        extra_test_args=[f"top.{r}" for r in roots] if sim not in ("icarus", "verilator") else [])
