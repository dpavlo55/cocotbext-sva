# Offline checking of a waveform dump


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

Checks concurrent assertions on a VCD or FST file after the simulation, with no
simulation overhead, for example when the simulator itself is slow or the
dump comes from another flow. Signals are sampled with the value they had
just before each clock edge (exact SVA sampling). `clock` is the default
clock: a property can name its own (`@(negedge clk) ...`, `@(posedge c iff
en)`) or several (multi-clock properties, with one tick per time step in
which one of their clocks ticks, as online). Real-valued signals are not
supported. Reading FST needs the compiled kernel
(`pip install cocotbext-sva[fast]`); the format is detected from the file.
FST dumps are much smaller than VCD, and checking them is faster. Dumps
written by nvc, GHDL, Icarus, Verilator (VCD and FST) and commercial
simulators (VCD) give the same results as checking online (vectors dumped one
bit per variable are reassembled). Make the
simulation run a little past the last clock edge you want checked: some
simulators do not write the last time step of a dump. Some write one more
time step than the test ran (GHDL, with clocks generated in the HDL):
`run(until_ns=...)` ignores what comes after the given time.
