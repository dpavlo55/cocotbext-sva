# cocotbext-sva

SystemVerilog-style **immediate and concurrent (temporal) assertions** for
[cocotb](https://www.cocotb.org) testbenches, written in SVA syntax. Works
with VHDL and Verilog designs. Requires cocotb 2.x and Python 3.10+.

```python
import cocotb
from cocotb.clock import Clock
from cocotbext.sva import Assertions, checked, posedge

@cocotb.test()
@checked                       # collect failures, fail the test at its end
async def test_handshake(dut):
    Clock(dut.clk, 10, unit="ns").start()
    a = Assertions(dut, clock=posedge(dut.clk), disable_iff="!rst_n", name="hs")
    a.assert_property("req |-> ##[1:3] ack", name="req_gets_ack")
    a.cover_property("req ##1 req ##1 req", name="three_reqs")
    ...                         # drive the design
```

## Install

```bash
pip install cocotbext-sva            # pure Python
pip install cocotbext-sva[fast]      # with the compiled kernel (same results, faster)
```

New here? Start with [Getting started](getting-started.md).

```{toctree}
:caption: User guide
:maxdepth: 2

getting-started
guide
```

```{toctree}
:caption: Reference
:maxdepth: 2

api
notes
```

```{toctree}
:caption: Development
:maxdepth: 2

design
release
```
