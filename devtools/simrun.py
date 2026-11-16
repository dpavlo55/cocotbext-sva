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
FST_SIMS = {"nvc", "ghdl", "icarus", "verilator"}   # write FST dumps


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

    # dump for offline checking: WAVE_VCD=<file> or WAVE_FST=<file>
    vcd, fst = os.environ.get("WAVE_VCD"), os.environ.get("WAVE_FST")
    if fst and sim not in FST_SIMS:
        sys.exit(f"{sim} does not write FST dumps")
    dump, fmt = (fst, "fst") if fst else (vcd, "vcd")
    build_args = []
    if sim == "verilator":
        if timing:
            build_args.append("--timing")
        if public:
            build_args.append("--public-flat-rw")
        build_args += ["-Wno-fatal", "-Wno-WIDTHEXPAND", "-Wno-WIDTHTRUNC"]
        if fst:
            build_args.append("--trace-fst")
        # extra local build options, e.g. include paths a Verilator install lacks
        build_args += os.environ.get("VERILATOR_EXTRA_ARGS", "").split()
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
        **({"waves": True} if sim == "verilator" and dump else {}),   # build with trace support
    )
    test_args = ["--std=08"] if sim == "ghdl" else []
    plusargs = []
    pre_cmd = None
    if dump:
        dump = Path(dump).resolve().as_posix()
        if sim == "ghdl":
            plusargs.append(f"--{fmt}={dump}")
        elif sim == "nvc":
            test_args += [f"--wave={dump}", f"--format={fmt}"]
        elif sim == "verilator":
            test_args += ["--trace", "--trace-file", dump]  # options of cocotb's main()
        elif sim == "icarus":
            plusargs.append(f"+vcd={dump}")            # $dumpvars block of the design
            # the runner appends -none (no dump) unless waves: ask for the format
            orig = runner._test_command
            runner._test_command = lambda: [[f"-{fmt}" if a == "-none" else a for a in c]
                                            for c in orig()]
        else:                                          # mixed-language simulators (VCD only)
            pre_cmd = [f"vcd file {{{dump}}}; vcd add -r /*"]
    results = runner.test(
        hdl_toplevel=toplevel,
        test_module=test_module,
        build_dir=build_dir,
        parameters=parameters or {},
        extra_env={**(extra_env or {}), "CALIB_SIM": sim, "CALIB_LANG": lang},
        test_args=test_args,
        plusargs=plusargs,
        **({"pre_cmd": pre_cmd} if pre_cmd else {}),
    )
    num_tests, num_failed = get_results(results)
    if num_tests == 0 or num_failed:
        sys.exit(f"{toplevel} failed on {sim}/{lang}: {num_failed}/{num_tests} tests failed")
    return build_dir
