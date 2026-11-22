import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from cocotbext.sva import Assertions, checked, posedge


@cocotb.test()
@checked  # fail the test at its end if an assertion failed
async def test_counter(dut):
    Clock(dut.clk, 10, unit="ns").start()

    # Assertions sampled on the rising edge of clk, switched off during reset
    a = Assertions(dut, clock=posedge(dut.clk), disable_iff="!rst_n", name="counter")
    a.assert_property("en |=> count == $past(count) + 1", name="counts_up")
    a.assert_property("!en |=> $stable(count)", name="holds")
    a.cover_property("en [*3]", name="three_in_a_row")

    # Reset for two cycles, then enable the counter at random
    dut.rst_n.value = 0
    dut.en.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    for _ in range(50):
        dut.en.value = random.randint(0, 1)
        await RisingEdge(dut.clk)
