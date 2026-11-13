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


@nox.session(python=False)
def smoke(session):
    """Minimal cocotb 2.x test on each simulator (VHDL and/or Verilog top)."""
    for sim in _selected_sims(session):
        session.log(f"--- smoke on {sim} ---")
        session.run(sys.executable, "smoke/run.py", env={"SIM": sim}, external=True)
