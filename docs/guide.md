# User guide

The README has the overview; this page has the details. Differences from
the reference implementation, simulator-specific behaviour and
measurements are in [notes.md](notes.md).

## Writing assertions

### Groups, clocks and names

```python
a = Assertions(
    scope=dut,                      # bare names are looked up here (case-insensitive for VHDL)
    clock=posedge(dut.clk),         # or "posedge clk", or "@(negedge clk iff en)"
    disable_iff="!rst_n",           # default disable iff
    name="top",                     # prefix in reports
    params={"DEPTH": 16},           # constants
    enums=StateEnum,                # IntEnum class or dict: state == S_BUSY
)
a.assert_property("u_ctrl.count <= DEPTH")                       # hierarchy
a.assert_property("@(posedge pclk) psel |=> penable")            # own clock in the text
a.assert_property("go |=> ##[0:15] fin",
                  signals={"go": dut.u_alu.op_start, "fin": dut.u_alu.op_done})
a.assert_property("out_valid |-> out_data == expected",
                  values={"expected": model.peek})               # Python value sampled per tick
```

Names are resolved when the assertion is created; typos fail immediately with
suggestions ("did you mean 'ack'?"). Enum literals use the integer values that
cocotb reads for the signal.

### Reuse

```python
from cocotbext.sva import Checker, Property

VALID_HELD = Property("valid && !ready |=> valid && $stable(data)", args=("valid", "ready", "data"))
a.assert_property(VALID_HELD(valid="in_valid", ready="in_ready", data=dut.in_data))

class StreamChecker(Checker):          # like an SV checker bound with `bind`
    ports = ("clk", "rst_n", "valid", "ready", "data")
    clock = "posedge clk"
    disable_iff = "!rst_n"

    def build(self, a):
        a.assert_property("valid && !ready |=> valid && $stable(data)", name="valid_held")

StreamChecker(a, name="in", clk=dut.clk, rst_n=dut.rst_n,
              valid=dut.in_valid, ready=dut.in_ready, data=dut.in_data)
StreamChecker(a, name="lane0", scope=dut.g_lane[0])   # ports found by name
a.sequence("burst4", "(valid && ready)[*4]")            # named sequence
a.let("hs", "valid && ready")                           # named expression
a.assert_property("burst4.triggered |=> !valid")        # true where a match of burst4 ends
```

`name.triggered` (or `name.ended`) of a named sequence is true at the ticks
where one of its matches ends, whatever tick the match started at. The
sequence is clocked like the place it is used in, unless it has a clocking
event of its own; it is then true in the time steps where a match ends on
that clock. Sequences with arguments or local variables cannot be used this
way.

### Immediate assertions

```python
a.check("count == 0", msg="counter not cleared")      # assert (expr) else $error(msg)
a.check(lambda: model.empty())                         # any Python predicate
a.check("!(full && empty)", deferred=True)             # assert #0: end of time step, last call wins
a.cover("state == S_DONE", name="done_seen")
```

### Temporal properties, single attempts, expect

```python
a.assert_property("start |=> busy until done")
a.assert_property("req |-> s_eventually [1:16] ack")
a.assert_property("accept_on (flush) req |-> ##[1:4] ack")     # a flush ends the obligation
a.assert_property("always !(busy && done)", initial=True)       # one attempt from the next tick
a.assert_property("s_eventually link_up", initial=True)         # fails at the end if never seen
ok = await a.expect("##[1:20] done", timeout=100)               # SV expect: wait for one evaluation
```

`initial=True` starts a single attempt at the next clock tick, like a
concurrent assertion in an SV `initial` block; that is what `always`,
`s_eventually` and `until` at the top of a property are usually meant for.
`expect` returns True when the property holds; a failure is reported like an
assertion failure and returns False. Its `timeout` (in clock ticks) ends a
still-pending evaluation with the end-of-test rule.

### Several clocks in one property

```python
a = Assertions(dut, clock=posedge(dut.clk_a))
a.assert_property("req |=> @(posedge clk_b) ##[1:3] ack_b")                 # clock crossing
a.assert_property("(send, v = data) ##1 @(posedge clk_b) (valid_b && data_b == v)[->1]")
a.assert_property("@(posedge clk_b) $rose(done_b) |-> @(posedge clk_a) busy")
```

A clocking event inside a property clocks what follows it, up to the next
clocking event, an implication's consequent included; it does not extend out
of enclosing parentheses. Between differently clocked sequences only `##1`
and `##0` are allowed (IEEE 1800 16.13):

- `s1 ##1 @(c2) s2` and `s1 |=> @(c2) p` continue at the first tick of `c2`
  strictly after the end of `s1`;
- `s1 ##0 @(c2) s2` and `s1 |-> @(c2) p` at the first tick of `c2` that is
  not before it: the same time step if both clocks tick in it.

Attempts start at the ticks of the leading clock. `$past`, `$rose` and the
other sampled-value functions follow the clock they are under, or the one
given as their last argument (`$rose(x, @(posedge clk_b))`). Named sequences
and properties may carry their own clocking event.

Multi-clock assertions are sampled with `sampling="preponed"`, so clocks
that tick in the same time step see the same values, and they are evaluated
at the end of the time step (failure callbacks cannot write signals). The
compiled kernel evaluates them (except single-attempt ones); their clocks
are always followed (no idle sleep).

### Sampling inside the simulator (`Probe`)

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

### Offline checking of a waveform dump

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

### Control, severity, results

```python
a.off("top.matches_model"); a.on("top.*"); a.kill("top.*")   # $assertoff / on / kill
a.assert_property(..., severity="fatal")    # info | warning | error (default) | fatal (stops now)
a.assert_property(..., on_fail=lambda f: dump(f))
async with Assertions(dut, ...) as a:       # check only inside the block
    ...
a.stats()                                   # per-assertion counters
```

## Supported SVA

| Area | Supported |
|---|---|
| Sequences | `##n`, `##[m:n]`, `##[m:$]`, `##[*]`, `##[+]`, `[*n]`, `[*m:n]`, `[*]`, `[+]`, `[->n]`, `[=n]`, `and`, `or`, `intersect`, `within`, `throughout`, `first_match`, local variables `(seq, v = e)`, `.triggered` / `.ended` |
| Properties | `|->`, `|=>`, `#-#`, `#=#`, `not`, `and`, `or`, `implies`, `iff`, `if`/`else`, `case`, `strong()`, `weak()`, `disable iff`, `nexttime`, `s_nexttime`, `always`, `s_always`, `eventually`, `s_eventually` (with ranges), `until`, `s_until`, `until_with`, `s_until_with`, `accept_on`, `reject_on`, `sync_accept_on`, `sync_reject_on` |
| Functions | `$past(e, n, gate)`, `$rose`, `$fell`, `$stable`, `$changed`, `$sampled`, `$onehot`, `$onehot0`, `$countones`, `$countbits`, `$isunknown`, `$signed`, `$unsigned`, `$bits`, `$clog2` |
| Expressions | SV operators (including `->` and `<->`) with SV width and sign rules, bit/part selects (VHDL `to`/`downto` ranges respected), concatenation, replication |
| Directives | `assert property`, `assume property`, `cover property` (each also as a single attempt: `initial=True`), `expect`, immediate and deferred immediate assert/assume/cover |
| Clocks | one clocking event per assertion (`posedge`, `negedge`, `edge`, `iff`), or several inside the property (multi-clock sequences and properties) |

## Semantics notes

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
  crash at exit ([notes.md](notes.md)).
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
[notes.md](notes.md).

## Speed and the compiled kernel

The package is pure Python. The optional compiled kernel (a separate
distribution, `cocotbext-sva-kernel`) is used automatically when installed,
and gives the same results:

```bash
pip install cocotbext-sva[fast]      # with the compiled kernel
```

Measured with nvc on Linux, in µs of wall time per clock cycle on top of the
simulator's own cost for a clock edge that resumes Python:

| | pure Python | with kernel |
|---|---|---|
| assertion with attempts in flight | ~0.5 | ~0.05 (+ ~2 per clock domain) |
| idle assertion | ~0.05 | ~0.05 |
| every assertion on a clock idle | ~0 (the clock is not followed: idle sleep) | ~0 |

Assertions with local variables and multi-clock assertions use the kernel
too: it keeps their attempts and calls back into Python only to compute
local values and test conditions on them (nvc, Linux: 10 local-variable
assertions with attempts in flight cost 6 µs per cycle with the kernel, as
many as 10 without local variables, and 20 µs without the kernel). Single-attempt
assertions (`initial=True`, `expect`) are evaluated in Python.
`COCOTBEXT_SVA_KERNEL=python` turns the kernel off; `native` requires it.
