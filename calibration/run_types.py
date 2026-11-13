"""Reader calibration on the simulator given by SIM (and TOPLEVEL_LANG)."""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
from devtools.simrun import run  # noqa: E402

run(here=here, toplevel="types", test_module="test_types", tag="types",
    vhdl=[here / "types.vhd"], verilog=[here / "types.sv"], public=False,
    extra_env={"CALIB_OUT": str(here / "types.jsonl")})
