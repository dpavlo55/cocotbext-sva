# Getting started

This page takes you from installing the package to a cocotb test with
concurrent assertions, and shows what a failing assertion looks like. It takes
about five minutes. The files are in
[`examples/getting_started`](https://github.com/dpavlo55/cocotbext-sva/tree/main/examples/getting_started)
of the repository.

## Requirements

- Python 3.10 or newer
- cocotb 2.x (installed with the package)
- a simulator supported by cocotb. The example runs as it is on nvc, GHDL
  (VHDL), Icarus Verilog and Verilator (SystemVerilog).

## Install

```bash
pip install cocotbext-sva
```

The optional compiled kernel gives the same results, faster
([details](guide/speed.md)):

```bash
pip install cocotbext-sva[fast]
```

## The design

An 8-bit counter: cleared while `rst_n` is low, counting up while `en` is high.
There is a VHDL and a SystemVerilog version; use either.

```{literalinclude} ../examples/getting_started/counter.vhd
:language: vhdl
:caption: counter.vhd
```

```{literalinclude} ../examples/getting_started/counter.sv
:language: systemverilog
:caption: counter.sv
```

## The test

```{literalinclude} ../examples/getting_started/test_counter.py
:language: python
:caption: test_counter.py
```

What each part does:

`Assertions(dut, clock=posedge(dut.clk), disable_iff="!rst_n", name="counter")`
: A group of assertions. Every assertion in it is sampled at the rising edge
  of `clk` and is disabled while `rst_n` is low, like `@(posedge clk) disable
  iff (!rst_n)` in SystemVerilog. Signal names in the properties are looked up
  in `dut`. `name` prefixes the assertion names in the log.

`a.assert_property("en |=> count == $past(count) + 1", name="counts_up")`
: A concurrent assertion, written in SVA: whenever `en` is high at a clock
  edge, `count` at the next edge must be one more than it was. A new attempt
  starts at every clock edge and runs in the background while the test drives
  the design.

`a.assert_property("!en |=> $stable(count)", name="holds")`
: When `en` is low, `count` must not change.

`a.cover_property("en [*3]", name="three_in_a_row")`
: Counts how often `en` was high for three clock edges in a row, to show that
  the test exercised that case.

`@checked`
: Collects the assertion failures of the test and fails the test at its end
  if there were any. Without it, failures are logged but the test passes.

## Run it

`run.py` builds the design and runs the test with cocotb's Python runner:

```{literalinclude} ../examples/getting_started/run.py
:language: python
:caption: run.py
```

```bash
cd examples/getting_started
python run.py                   # nvc
SIM=icarus python run.py        # Icarus Verilog (also: ghdl, verilator)
```

In PowerShell, set the simulator with `$env:SIM = "icarus"` before
`python run.py`.

The test passes, and at its end the package logs a summary of every
assertion:

```text
assertion summary (counter): 3 concurrent, 0 immediate, 0 failure(s)
  counter.counts_up        assert  attempts=52 pass=23 vacuous=26 fail=0 disabled=2
  counter.holds            assert  attempts=52 pass=26 vacuous=24 fail=0 disabled=2
  counter.three_in_a_row   cover    hits=6
...
** TEST                          STATUS  SIM TIME (ns)  REAL TIME (s)  RATIO (ns/s) **
** test_counter.test_counter      PASS         510.00           0.02      31875.00  **
** TESTS=1 PASS=1 FAIL=0 SKIP=0                510.00           0.06       8095.24  **
```

*Vacuous* attempts are those where the left side of the implication was false,
so nothing was checked; *disabled* attempts fell in the reset (`disable iff`).
An assertion with only vacuous successes is reported as "never triggered".
The test drives `en` at random, so the counts change from run to run.

## See a failure

Break the first property on purpose: change `+ 1` to `+ 2` in
`test_counter.py` and run again. Each failure is logged at the clock edge
where it happens, with the time the failing attempt started and the values
sampled there:

```text
30.00ns ERROR    sva.counter.counts_up      FAILED  attempt started 20 ns
                                                en |=> count == $past(count) + 2
                                                sampled: rst_n=1  en=1  count=1
```

At the end, `@checked` fails the test:

```text
AssertionError: 26 assertion failure(s) in 1 assertion(s): counter.counts_up
** test_counter.test_counter      FAIL         510.00           0.00           inf  **
```

Change it back to `+ 1` before going on.

The package logs on the `sva` logger, at INFO level by default. To keep only
warnings and failures (no summary), add this to the test module:

```python
import logging

logging.getLogger("sva").setLevel(logging.WARNING)
```

## Next steps

- [Writing assertions](guide/assertions.md): clocks and names,
  reusable properties and checkers, immediate assertions, `expect`, several
  clocks in one property.
- [Supported SVA](guide/supported-sva.md): the operators and functions you can
  use.
- [`Probe`](guide/probe.md): lower overhead by
  sampling inside the simulator.
- [Offline checking](guide/offline.md): check
  assertions on a VCD or FST dump after the simulation.
- [API reference](api.md): every class and function.
