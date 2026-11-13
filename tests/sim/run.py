"""Integration tests on the simulator given by SIM (and TOPLEVEL_LANG)."""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parents[1]))
from devtools.simrun import run  # noqa: E402

run(here=here, toplevel="dut", test_module="test_sim", tag="sim",
    vhdl=[here / "dut.vhd"], verilog=[here / "dut.sv"])
