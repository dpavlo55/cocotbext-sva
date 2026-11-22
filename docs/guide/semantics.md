# Semantics notes


- **Sampling.** SVA samples the values from before the time step of the
  clock edge. Reading them when the edge is seen (`sampling="edge"`) gives
  exactly that for clocks driven from cocotb (`cocotb.clock.Clock`) on all
  tested simulators, but not for clocks derived or generated inside the HDL.
  `sampling="preponed"` takes the values at the start of every time step
  instead: exact for any clock, at about 10 µs more per clock cycle on the
  fast simulators. The default, `"auto"`, compares the two during the first
  256 ticks of a clock (and briefly again every 4096 ticks) and uses
  `preponed` from the first difference on, `edge` otherwise:

  ```python
  a = Assertions(dut, clock=posedge(dut.clk_div, sampling="preponed"))   # one clock
  a = Assertions(dut, clock="posedge clk_div", sampling="preponed")      # a group
  ```

  `COCOTBEXT_SVA_SAMPLING=edge|preponed|auto` sets the default. Known limit:
  on Verilator, if the last test of a simulation ends in the read-write or
  read-only phase while a preponed clock is being followed, the simulator can
  crash at exit ([notes.md](../notes.md)).
- **X/Z.** Unknown bits read as 0 in expressions; `$isunknown` and
  `$countbits` see them. Verilator is 2-state.
- **`disable iff`, `accept_on`, `reject_on`** are evaluated at clock ticks,
  not asynchronously (so `accept_on` and `sync_accept_on` behave alike). An
  abort condition decides the attempt at that tick, whatever the operand's
  own verdict, except that a vacuous success stays vacuous.
- **Strength.** Sequences used as properties are weak in `assert`/`assume`
  and strong in `cover`; inside a strong operator (`s_eventually`,
  `s_always`, `s_until`, `s_nexttime`) they are strong. Pending strong
  obligations fail at the end of the test, including the consequent of a
  `|=>` whose antecedent matched at the last tick when it is strong. Such a
  failure has `at_end=True` and the time of `finalize()` (offline: the
  `until_ns` given to `run()`, else the last clock tick of the dump).
- **`|=>` with an empty antecedent match** (e.g. `a[*0:1] |=> b`): the
  consequent starts at the next tick; for `|->` empty matches are ignored.
- **Vacuity** follows IEEE 1800 16.14.8 where the reference implementation
  agrees; `always` and `eventually` successes always count as nonvacuous.
  `$past` before enough history returns the value at the moment the
  assertion was created.
- **Operators after a clock change.** `nexttime`, `always`, `eventually`,
  `until`, `not` and `#-#` that follow a clock change (`a |=> @(c2) nexttime
  b`) are evaluated from the next tick of their own clock on, as the formal
  semantics of IEEE 1800 defines clocked properties: `nexttime` then moves
  one tick further, a range `[0:n]` starts counting there. The reference
  simulator used for the golden corpus counts from the antecedent's last
  tick instead (it checks `b` at the first tick of `c2`). Sequences,
  implications and `if` after a clock change agree with it.
- **Speed.** While every assertion on a clock is idle (nothing in flight),
  the package stops evaluating each edge and only counts edges until a signal
  the idle assertions read changes; counters stay exact. Set
  `COCOTBEXT_SVA_IDLE_SLEEP=0` to turn this off.

Behaviour is checked against a literal reference interpreter (differential
fuzzing) and against a golden corpus of results from a mature SVA
implementation (`tests/corpus`), multi-clock properties included. The cases
where they differ, simulator-specific behaviour and measurements are in
[notes.md](../notes.md).
