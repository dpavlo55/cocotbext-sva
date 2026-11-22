"""The probes of the probe test (used by run_probe.py to generate the HDL and
by test_probe.py in the simulation)."""
import os

from cocotbext.sva import Probe

VHDL = os.environ.get("TOPLEVEL_LANG", os.environ.get("CALIB_LANG", "verilog")) == "vhdl"
SIGNALS = {"rst_n": 1, "req": 1, "ack": 1, "start": 1, "en": 1, "busy": 1, "done": 1, "rsp": 1,
           "tag": 4, "rtag": 4, "count": 8}
# VHDL: the probes sit beside the DUT in a wrapper (dut_probe.vhd); SystemVerilog: bound into it
TOP = dict(top="dut_probe", scope="u_dut") if VHDL else dict(top="dut")


def make(vhdl=VHDL):
    top = dict(top="dut_probe", scope="u_dut") if vhdl else dict(top="dut")
    return {d: Probe(f"p{d}", "posedge clk", SIGNALS, depth=d, **top) for d in (1, 16)}
