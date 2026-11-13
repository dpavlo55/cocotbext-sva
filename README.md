# cocotbext-sva

> **Note:** this package was implemented with [Claude Code](https://claude.com/claude-code),
> and its results were validated against a commercial SVA simulator.

SystemVerilog-style **immediate and concurrent (temporal) assertions** for
[cocotb](https://www.cocotb.org) testbenches, written in SVA syntax. Works
with VHDL and Verilog designs.

Status: in development (not released). Requires cocotb 2.x and Python 3.10+.

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
    a.assert_property("(req, t = tag) |-> ##[1:8] (rsp && rsp_tag == t)", name="tag_returns")
    a.cover_property("req ##1 req ##1 req", name="three_reqs")
    ...                         # drive the design
```

A failure is logged at the time it happens, with the attempt's start time and
the sampled values:

```
   150.00ns ERROR    sva.hs.req_gets_ack   FAILED  attempt started 120 ns
                                               req |-> ##[1:3] ack
                                               sampled: req=0  ack=0
```

## Features

- **The SVA property language:** sequences, implications, local variables,
  `$past` and the other sampled-value functions, `always` / `eventually` /
  `until`, `disable iff`, `accept_on`, `cover`, `expect`, immediate
  assertions ([full list](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/guide.md#supported-sva)).
- **Several clocks:** clocks generated or derived in the HDL (exact SVA
  sampling), and multi-clock properties (`req |=> @(posedge clk_b) ack_b`).
- **Reuse:** parameterised properties, named sequences, checkers bound to
  several instances.
- **Speed:** all attempts of an assertion are merged and evaluated together;
  an optional compiled kernel makes a busy assertion cost about 0.05 µs per
  cycle; idle clocks are not followed at all.
- **`Probe`:** an optional generated HDL module that samples the signals
  inside the simulator and buffers them, for the lowest overhead.
- **Offline checking:** the same assertions on a VCD or FST dump after the
  simulation (`TraceSampler`).

## Install

```bash
pip install cocotbext-sva            # pure Python
pip install cocotbext-sva[fast]      # with the compiled kernel (same results, faster)
```

## Validation

- Tested on nvc, GHDL, Icarus Verilog and Verilator, and on a commercial
  simulator (VHDL and SystemVerilog).
- Results checked against a golden corpus of about 1,200 cases recorded from
  a commercial SVA implementation, and fuzzed against a reference
  interpreter of the IEEE 1800 formal semantics. Where the commercial tool
  and the standard disagree, the package follows the standard; the cases are
  listed in [docs/notes.md](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/notes.md).

## Documentation

- [User guide](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/guide.md): writing assertions, clocks and sampling,
  `Probe`, offline checking, semantics notes, speed.
- [Notes](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/notes.md): differences from the reference implementation,
  simulator-specific behaviour, measurements.
- [Design](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/design.md): architecture. [Releasing](https://github.com/dpavlo55/cocotbext-sva/blob/main/docs/release.md).

## Development

```bash
pip install -r requirements.txt
nox -s kernel                # build and install the compiled kernel (needs Rust)
nox -s tests                 # unit tests, fuzzing, golden corpus (no simulator)
nox -s simtests              # end-to-end tests on every simulator found
```

CI runs on GitHub Actions (`.github/workflows`).

License: MIT (see `LICENSE`).
