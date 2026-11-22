# Writing assertions

## Groups, clocks and names

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

## Reuse

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

## Immediate assertions

```python
a.check("count == 0", msg="counter not cleared")      # assert (expr) else $error(msg)
a.check(lambda: model.empty())                         # any Python predicate
a.check("!(full && empty)", deferred=True)             # assert #0: end of time step, last call wins
a.cover("state == S_DONE", name="done_seen")
```

## Temporal properties, single attempts, expect

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

## Control, severity, results

```python
a.off("top.matches_model"); a.on("top.*"); a.kill("top.*")   # $assertoff / on / kill
a.assert_property(..., severity="fatal")    # info | warning | error (default) | fatal (stops now)
a.assert_property(..., on_fail=lambda f: dump(f))
async with Assertions(dut, ...) as a:       # check only inside the block
    ...
a.stats()                                   # per-assertion counters
```

At the end of a test (or of the `async with` block) a summary of every
assertion is logged: attempts, passes, vacuous successes, failures and cover
hits. Everything is logged on the `sva` logger, failures on `sva.<name>`, at
INFO level by default; `logging.getLogger("sva").setLevel(logging.WARNING)`
leaves out the summary and the other INFO messages.
