"""Smoke test: python smoke/run.py   (simulator chosen with env var SIM, default nvc)."""
import os
import sys
from pathlib import Path

from cocotb_tools.check_results import get_results
from cocotb_tools.runner import get_runner

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))  # test module importable while running in build_dir
sim = os.environ.get("SIM", "nvc")
src = [here / "counter.vhd"] if sim in ("nvc", "ghdl") else [here / "counter.sv"]
lang = os.environ.get("TOPLEVEL_LANG", "vhdl" if src[0].suffix == ".vhd" else "verilog")
if lang == "vhdl":
    src = [here / "counter.vhd"]

runner = get_runner(sim)
build_dir = here / "sim_build" / f"{sim}_{lang}"
runner.build(sources=src, hdl_toplevel="counter", build_dir=build_dir, always=True,
             timescale=("1ns", "1ps"))
results = runner.test(hdl_toplevel="counter", test_module="test_counter", build_dir=build_dir)
num_tests, num_failed = get_results(results)
if num_tests == 0 or num_failed:
    sys.exit(f"smoke failed on {sim}: {num_failed}/{num_tests} tests failed")

