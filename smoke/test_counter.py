import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


@cocotb.test()
async def counts(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    dut.en.value = 1
    await ClockCycles(dut.clk, 10)
    await RisingEdge(dut.clk)
    assert int(dut.count.value) in (10, 11), int(dut.count.value)
