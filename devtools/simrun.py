"""Shared build-and-run helper for the smoke test, calibration and benchmarks.

Simulator comes from the SIM environment variable (default: nvc), language from
TOPLEVEL_LANG (default: vhdl for VHDL-only simulators, verilog otherwise, unless
only one of the two sources is given).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from cocotb_tools.check_results import get_results
from cocotb_tools.runner import get_runner

VHDL_ONLY = {"nvc", "ghdl"}
VERILOG_ONLY = {"icarus", "verilator"}


def selected_sim() -> str:
    return os.environ.get("SIM", "nvc")


def selected_lang(sim: str, vhdl: list[Path], verilog: list[Path]) -> str:
    lang = os.environ.get("TOPLEVEL_LANG")
    if lang:
        return lang
    if sim in VHDL_ONLY or not verilog:
        return "vhdl"
    if sim in VERILOG_ONLY or not vhdl:
        return "verilog"
    return "verilog"


def run(
    *,
    here: Path,
    toplevel: str,
    test_module: str,
    vhdl: list[Path] = (),
    verilog: list[Path] = (),
    parameters: dict | None = None,
    extra_env: dict | None = None,
    tag: str = "",
    timing: bool = False,
    public: bool = True,
) -> Path:
    """Build and run one cocotb test module; exit non-zero if any test failed.

    timing: design contains delays (#5 / after 5 ns) -> Verilator needs --timing.
    public: make all signals visible to cocotb (Verilator --public-flat-rw).
    Returns the test (build) directory.
    """
    here = here.resolve()
    sys.path.insert(0, str(here))  # test module importable while running in build_dir
    sim = selected_sim()
    lang = selected_lang(sim, list(vhdl), list(verilog))
    sources = list(vhdl) if lang == "vhdl" else list(verilog)
    if not sources:
        sys.exit(f"no {lang} sources for {toplevel}")

    build_args = []
    if sim == "verilator":
        if timing:
            build_args.append("--timing")
        if public:
            build_args.append("--public-flat-rw")
        build_args += ["-Wno-fatal", "-Wno-WIDTHEXPAND", "-Wno-WIDTHTRUNC"]
    if sim == "nvc":
        build_args.append("--std=2008")
    if sim == "ghdl":
        build_args.append("--std=08")

    runner = get_runner(sim)
    build_dir = here / "sim_build" / f"{sim}_{lang}{('_' + tag) if tag else ''}"
    runner.build(
        sources=sources,
        hdl_toplevel=toplevel,
        build_dir=build_dir,
        always=True,
        build_args=build_args,
        parameters=parameters or {},
        timescale=("1ns", "1ps"),
    )
    test_args = ["--std=08"] if sim == "ghdl" else []
    plusargs = []
    if sim == "ghdl" and os.environ.get("WAVE_VCD"):
        plusargs.append(f"--vcd={os.environ['WAVE_VCD']}")      # dump for offline checking
    results = runner.test(
        hdl_toplevel=toplevel,
        test_module=test_module,
        build_dir=build_dir,
        parameters=parameters or {},
        extra_env={**(extra_env or {}), "CALIB_SIM": sim, "CALIB_LANG": lang},
        test_args=test_args,
        plusargs=plusargs,
    )
    num_tests, num_failed = get_results(results)
    if num_tests == 0 or num_failed:
        sys.exit(f"{toplevel} failed on {sim}/{lang}: {num_failed}/{num_tests} tests failed")
    return build_dir
