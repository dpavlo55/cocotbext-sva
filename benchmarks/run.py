"""Phase 0 benchmarks on the simulator given by SIM (and TOPLEVEL_LANG)."""
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
from devtools.simrun import run  # noqa: E402

out = here / os.environ.get("BENCH_RESULTS", "results.jsonl")
run(here=here, toplevel="bench", test_module="test_bench",
    vhdl=[here / "bench.vhd"], verilog=[here / "bench.sv"],
    public=False,  # only the probed signals are public (verilator comments in bench.sv)
    extra_env={"BENCH_OUT": str(out),
               "BENCH_CYCLES": os.environ.get("BENCH_CYCLES", "20000")})
