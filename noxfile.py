"""Task runner, identical on Windows, WSL/Linux and (later) CI.

    nox -s smoke                  # every open-source simulator found on PATH
    nox -s smoke -- nvc icarus    # only the named simulators (any cocotb SIM name)

Sessions run in the already-active environment (python=False): no per-session
virtualenvs, so they start instantly. Install requirements.txt first.
"""
import os
import shutil
import sys

import nox

nox.options.default_venv_backend = "none"

# cocotb SIM name -> executable that must be on PATH
OPEN_SOURCE_SIMS = {"nvc": "nvc", "ghdl": "ghdl", "icarus": "iverilog", "verilator": "verilator"}


def _selected_sims(session):
    if session.posargs:
        return list(session.posargs)
    found = [sim for sim, exe in OPEN_SOURCE_SIMS.items() if shutil.which(exe)]
    if not found:
        session.error("no simulator found on PATH")
    return found


VHDL_ONLY = {"nvc", "ghdl"}
VERILOG_ONLY = {"icarus", "verilator"}


def _langs(sim):
    if sim in VHDL_ONLY:
        return ["vhdl"]
    if sim in VERILOG_ONLY:
        return ["verilog"]
    return ["vhdl", "verilog"]  # mixed-language simulators: run both tops


def _run_everywhere(session, script):
    for sim in _selected_sims(session):
        for lang in _langs(sim):
            session.log(f"--- {script} on {sim}/{lang} ---")
            session.run(sys.executable, script, env={"SIM": sim, "TOPLEVEL_LANG": lang},
                        external=True)


@nox.session(python=False)
def calibrate(session):
    """Phase 0: sampling calibration (pre- vs post-edge reads per clock kind)."""
    for out in ("calibration/results.jsonl", "calibration/types.jsonl"):
        if os.path.exists(out):
            os.remove(out)
    _run_everywhere(session, "calibration/run.py")
    session.run(sys.executable, "calibration/report.py", external=True)
    for sim in _selected_sims(session):
        for lang in _langs(sim):
            session.run(sys.executable, "calibration/run_types.py",
                        env={"SIM": sim, "TOPLEVEL_LANG": lang}, external=True, success_codes=[0, 1])
    session.run(sys.executable, "calibration/report_types.py", external=True)


@nox.session(python=False)
def bench(session):
    """Phase 0: read cost and simulation speed under Python load."""
    out = "benchmarks/results.jsonl"
    if os.path.exists(out):
        os.remove(out)
    _run_everywhere(session, "benchmarks/run.py")
    session.run(sys.executable, "benchmarks/report.py", external=True)


@nox.session(python=False)
def kernel(session):
    """Build the compiled kernel (kernel/, Rust) and install it in this environment."""
    session.chdir("kernel")
    session.run("maturin", "develop", "--release", external=True)


@nox.session(python=False)
def tests(session):
    """Unit tests and differential fuzzing (no simulator); with the compiled
    kernel installed, once with it and once in pure Python."""
    session.run(sys.executable, "-m", "pytest", "-q", "tests", *session.posargs, external=True)
    try:
        import cocotbext_sva_kernel  # noqa: F401
    except ImportError:
        return
    session.run(sys.executable, "-m", "pytest", "-q", "tests", *session.posargs, external=True,
                env={"COCOTBEXT_SVA_KERNEL": "python"})


@nox.session(python=False)
def simtests(session):
    """End-to-end tests of the package on simulators, then offline checking of
    a dump each simulator writes (must equal the online results)."""
    _run_everywhere(session, "tests/sim/run.py")
    _run_everywhere(session, "tests/sim/run_offline.py")


@nox.session(python=False)
def smoke(session):
    """Minimal cocotb 2.x test on each simulator (VHDL and/or Verilog top)."""
    for sim in _selected_sims(session):
        session.log(f"--- smoke on {sim} ---")
        session.run(sys.executable, "smoke/run.py", env={"SIM": sim}, external=True)
