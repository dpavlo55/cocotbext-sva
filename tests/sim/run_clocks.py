"""Tests with clocks generated or derived inside the HDL, and several clocks."""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parents[1]))
from devtools.simrun import run  # noqa: E402

run(here=here, toplevel="clocks", test_module="test_clocks", tag="clocks",
    vhdl=[here / "clocks.vhd"], verilog=[here / "clocks.sv"], timing=True)
