"""SystemVerilog-style immediate and concurrent assertions for cocotb.

    from cocotbext.sva import Assertions, checked, posedge

    @cocotb.test()
    @checked
    async def test_handshake(dut):
        a = Assertions(dut, clock=posedge(dut.clk), disable_iff="!rst_n")
        a.assert_property("req |-> ##[1:3] ack", name="req_gets_ack")
        ...
"""
from ._api import (AssertionHandle, Assertions, BoundProperty, Checker, ClockingEvent, Failure,
                   Property, Sequence, checked, edge, negedge, posedge)
from ._errors import SvaError, SvaNameError, SvaSyntaxError, SvaUnsupportedError
from ._offline import OfflineFailure, OfflineResult, TraceSampler

__all__ = [
    "Assertions", "AssertionHandle", "BoundProperty", "Checker", "ClockingEvent", "Failure",
    "Property", "Sequence", "checked", "edge", "negedge", "posedge",
    "SvaError", "SvaNameError", "SvaSyntaxError", "SvaUnsupportedError",
    "TraceSampler", "OfflineResult", "OfflineFailure",
]
__version__ = "0.1.0.dev0"
