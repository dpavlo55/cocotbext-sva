"""Sampling calibration on the simulator given by SIM (and TOPLEVEL_LANG)."""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
from devtools.simrun import run  # noqa: E402

out = here / "results.jsonl"
run(here=here, toplevel="calib", test_module="test_calib",
    vhdl=[here / "calib.vhd"], verilog=[here / "calib.sv"],
    timing=True, extra_env={"CALIB_OUT": str(out)})
