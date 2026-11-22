# Sampling inside the simulator (`Probe`)


A `Probe` generates a small HDL module that stores the signals of a group of
assertions at every clock tick, inside the simulator, in a buffer of `depth`
samples. Python then reads one vector per wake-up instead of one value per
signal, and with `depth > 1` it wakes once every `depth` ticks instead of at
every tick.

```python
# shared by the build script and the test
PROBE = Probe("hs", "posedge clk", {"req": 1, "ack": 1, "count": 8}, top="dut", depth=64)

# build script: generate the probe and compile it with the design
PROBE.write("build/sva_probe_hs.sv")        # or .vhd

# test
a = Assertions(dut, clock=PROBE.attach(dut), disable_iff="!rst_n")
a.assert_property("req |-> ##[1:3] ack")
```

- Results are those of sampling at the clock edge. With `depth > 1` a
  failure is logged up to `depth` ticks late, with its exact time in the
  message and in `Failure.time`; `stats()` and `finalize()` first evaluate
  what is in the buffer.
- The signals are taken as a flip-flop on that clock takes them. The sample
  of a tick is in the buffer after the clock edge's updates: a test that ends
  in the time step of a clock edge does not see that tick.
- Only the probe's signals can be used (no `values=`, no clocking events in
  the property), with their declared widths.
- Getting the probe into the design:

  | | How |
  |---|---|
  | SystemVerilog | The file ends with a `bind` into the top-level module (`write(path)`). Where that does not work, `write(path, bind=False)` makes the probe a top-level module to elaborate beside the design: Icarus (`-s sva_probe_hs`), and simulators that do not elaborate a `bind` at file level. |
  | VHDL-2008 | Instantiate `entity work.sva_probe_hs` after the design in a testbench architecture; it reads the signals through external names. Types other than `std_logic(_vector)` are declared in the signal table: `{"cnt": (8, "unsigned"), "st": (2, "enum state_t")}`. |
  | VHDL, GHDL | `write(path, interface="ports")`: the entity has one port per signal, to be mapped in the instantiation (GHDL does not start under cocotb with external names in the design). |

Measured with 50 assertions with attempts in flight (µs per cycle above the
simulation without Python, Linux): nvc 9.1 at the clock edge, 3.9 through a
probe of depth 64; Verilator 10.1 and 5.0. A probe of depth 1 costs 1 to 1.5
µs more than sampling at the clock edge.
