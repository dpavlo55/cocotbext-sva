# Offline checking of a waveform dump

`TraceSampler` checks concurrent assertions on a VCD or FST file after the
simulation, instead of while it runs. The assertions are written exactly as
online, and give the same results.

Use it when:

- the simulation should run at full speed, with no Python work per clock
  cycle;
- the dump comes from another flow (a regression run, a colleague, a
  simulator you do not drive from cocotb);
- you want to add or change assertions and check them again without running
  the simulation again.

## An example

```python
from cocotbext.sva import TraceSampler

ts = TraceSampler("waves.vcd", clock="posedge clk", scope="tb.dut", disable_iff="!rst_n")   # or .fst
ts.assert_property("req |-> ##[1:3] ack", name="req_gets_ack")
ts.cover_property("req ##1 req ##1 req", name="three_reqs")
ts.assert_property("req |=> @(posedge clk_b) ##[1:4] ack_b", name="crossing")   # several clocks
result = ts.run()            # run(until_ns=...) stops at that time
print(result.summary())
result.check()               # AssertionError listing the failures, if any
```

`clock` is the default clock of the assertions, and `scope` is the instance in
the dump under which the names are looked up: dot-separated, as the dump
writes it, and case-insensitive if there is no exact match. `disable_iff` is
the default `disable iff` of the assertions (covers are not disabled by it).

`run()` reads the dump once and checks every assertion added so far.

## Reading the results

`run()` returns an `OfflineResult`. `result.ok` is true when nothing failed;
`result.failures` lists the failures and `result.stats` the counters of each
assertion. `summary()` puts it all in a readable report. For the counter of
[Getting started](../getting-started.md), dumped by nvc, with an extra
assertion `count < 20` that is meant to fail:

```text
51 clock cycles checked, 3 failure report(s)
  counts_up: 51 attempts, 22 passed, 28 vacuous, 0 failed
  holds: 51 attempts, 27 passed, 22 vacuous, 0 failed
  small: 51 attempts, 47 passed, 0 vacuous, 3 failed
  three_in_a_row: cover, 4 hit(s) in 51 attempts
  small FAILED at 490 ns, attempt started 490 ns
  small FAILED at 500 ns, attempt started 500 ns
  small FAILED at 510 ns, attempt started 510 ns
```

`result.check()` raises an `AssertionError` listing the failures, so a test
or a script fails when an assertion does.

## Clocks and sampling

Signals are sampled with the value they had just before each clock edge,
which is exactly what SVA samples, whatever drives the clock.

A property can name its own clock (`@(negedge clk) ...`, `@(posedge c iff
en)`) instead of the default one, or several clocks (multi-clock properties,
with one tick per time step in which one of their clocks ticks, as online).

## VCD and FST

Both formats are supported; the format is detected from the file's content,
not its name. FST dumps are much smaller than VCD, and checking them is
faster. Reading FST needs the compiled kernel:

```bash
pip install cocotbext-sva[fast]
```

Dumps written by nvc, GHDL, Icarus and Verilator (VCD and FST) and by
commercial simulators (VCD) give the same results as checking online.
Vectors that a simulator dumps one bit per variable are put back together.

## The end of the dump

Simulators do not all stop the dump where the test stopped:

- Some do not write the last time step: make the simulation run a little past
  the last clock edge you want checked.
- Some write one more time step than the test ran (GHDL, with clocks
  generated in the HDL). `run(until_ns=...)` checks only up to the given
  time; give it the time the test ended.

Strong obligations still pending at the end (for example an `s_eventually`
that never matched) fail at that time, as at the end of a test online.

## Limits

- Real-valued signals are not supported.
- A `Property` with arguments can be used, but offline its actual arguments
  are names or expressions in the dump (strings), not signal handles.
- There are no immediate assertions offline; check single conditions with a
  concurrent assertion, which is checked at every clock tick.
