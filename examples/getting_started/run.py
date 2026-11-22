"""Build the counter and run test_counter.py.

    python run.py                      # nvc, VHDL
    SIM=icarus python run.py           # Icarus Verilog
    SIM=verilator python run.py        # Verilator

SIM is any simulator name cocotb knows; the VHDL design is used for nvc and
GHDL, the SystemVerilog one otherwise (TOPLEVEL_LANG=vhdl|verilog overrides).
"""
import os
import sys
from pathlib import Path

from cocotb_tools.check_results import get_results
from cocotb_tools.runner import get_runner

here = Path(__file__).resolve().parent
sim = os.environ.get("SIM", "nvc")
lang = os.environ.get("TOPLEVEL_LANG", "vhdl" if sim in ("nvc", "ghdl") else "verilog")
source = here / ("counter.vhd" if lang == "vhdl" else "counter.sv")

runner = get_runner(sim)
build_dir = here / "sim_build" / f"{sim}_{lang}"
runner.build(sources=[source], hdl_toplevel="counter", build_dir=build_dir,
             timescale=("1ns", "1ps"), always=True)
sys.path.insert(0, str(here))  # so the simulator finds test_counter.py
results = runner.test(hdl_toplevel="counter", test_module="test_counter",
                      build_dir=build_dir)
num_tests, num_failed = get_results(results)
sys.exit(1 if num_tests == 0 or num_failed else 0)
