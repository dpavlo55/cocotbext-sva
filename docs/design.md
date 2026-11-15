# cocotbext-sva — design

Status: phase 1 implemented. This document records the design decisions
made before the first line of the package was written. It is the reference for
contributors; update it when a decision changes.

## 1. Goal

Bring SystemVerilog assertions to cocotb testbenches, in Python, for designs in
any HDL that cocotb supports:

- immediate assertions (`assert`, `assume`, `cover`, deferred `#0` / `final`);
- concurrent (temporal) assertions written in **SVA syntax**, covering almost
  all useful SVA operators, sampled-value functions and directives;
- fast enough to leave on in regressions.

Out of scope for now: PSL syntax, `checker` blocks, formal verification.

## 2. Requirements

| # | Requirement | Consequence |
|---|---|---|
| R1 | Speed is critical | Compiled automata, shared sampling loop, demand-driven reads (§5, §11) |
| R2 | Each failure is reported **at the simulation time it happens** | Synchronous evaluation is the default; batching is opt-in only |
| R3 | Failures do **not** stop the test; they are collected and the test fails at its end | `@checked` / `async with` finalization (§7.3); `$fatal` severity still stops immediately |
| R4 | cocotb 2.x only | No 1.x compatibility layer; use the 2.x value and trigger APIs |
| R5 | Direct mode (no HDL changes) is the default | Correct sampling and fast reads must be achieved from Python alone |
| R6 | Works on nvc, GHDL, Verilator, Icarus (and other cocotb-supported simulators) | Per-simulator calibration tests; CI matrix |
| R7 | Open-source package for everyone | Must handle many clock domains, both edges, VHDL and Verilog conventions; BSD-3-Clause |
| R8 | SVA syntax only (for now) | Hand-written SVA parser; internal representation is syntax-neutral |

## 3. Architecture overview

```
 SVA text / builder API
          │  front end (once, at test start)
          ▼
   parse → IR (syntax tree, every node carries its clocking event)
          │  compiler (once)
          ▼
   atomic conditions → numbered input bits
   sequences → NFA (Thompson construction; and/intersect/within as products)
   properties → attempt-merging automaton
   condition evaluation → generated Python source, exec'd
          │  runtime (every sampled clock event)
          ▼
   Sampler ──(cycle, time, sampled values)──▶ Engine ──events──▶ Reporter
```

The **engine never touches cocotb**. It consumes a stream of sampled values per
clocking event, produced by a pluggable `Sampler`. This one seam makes the
engine testable without a simulator, enables offline checking of waveform
dumps, and lets new sampling strategies (gated clocks, HDL probes) be added
without touching the engine.

| Sampler | Phase | Purpose |
|---|---|---|
| `EdgeSampler` | 1 | Reads at the clock-edge callback. Correct for clocks driven from cocotb |
| `TraceSampler` | 1 (tests), 3 (users) | Replays traces / FST / VCD; engine unit tests and offline checking |
| `PreponedSampler` | 4 | Correct sampling for clocks generated or gated inside the HDL |
| `ProbeSampler` | 4 | Optional generated HDL wrapper: HDL-side sampling, one packed read, batching |

**Clocking event** is an object, not a signal: signal + edge
(`posedge` / `negedge` / `edge`) + optional `iff` condition. All assertions on
the same clocking event share one sampling loop.

**Multi-clock readiness:** every IR node carries its clocking event from day
one, even though v1 accepts only one clock per property. Multi-clock sequences
(`@(posedge a) x ##1 @(posedge b) y`) and `$past(..., @(clk))` are then a later
extension of the compiler, not a rewrite.

## 4. Front end

- Hand-written recursive-descent parser with precedence climbing, following the
  operator precedence table of IEEE 1800. Chosen for precise error messages
  (caret under the offending token) and zero dependencies.
- A Python builder API (`seq(req).delay(1, 3).then(ack)`) produces the same IR.
- Every IR node keeps its source span, so failures can underline the
  sub-expression that failed.

### 4.1 Name resolution

Names in SVA text are resolved **once, when the assertion is created**, in this
order:

1. `signals=` (explicit handles / aliases)
2. `values=` (Python callables sampled at the clock event)
3. `params=` (constants, folded into generated code)
4. named sequences / properties
5. local variables
6. the `scope` hierarchy (case-insensitive for VHDL)

Unresolved names fail at test setup with close-match suggestions. Typing rules:
`std_logic` / `bit` are booleans; vectors are unsigned (`$signed()` available);
VHDL enum literals compile to integer compares; `sig[7:0]` follows the declared
range direction (`to` / `downto`); record fields as `rec.field` where the
simulator exposes them. Expression operators are SV's (`==`, `!=`, `&&`) for
both languages.

## 5. Engine

### 5.1 Fast path — attempt-merging lazy DFA

For properties without local variables, an attempt's entire future depends only
on its position in the property. Attempts at the same position are
interchangeable, so the set of all in-flight attempts is one state of a
deterministic automaton (subset construction over the NFA).

- The DFA is built **lazily** (as RE2 does): a transition is computed the first
  time a (state, input-bits) pair occurs, then cached together with its events
  (pass, fail, vacuous pass, cover hit, source span of a failure).
- Per assertion per cycle: one generated predicate call + one dict lookup,
  **independent of the number of attempts in flight**.
- Each DFA state also stores, cheaply, the start cycle of its oldest attempt and
  the number of merged attempts, for failure reports.

### 5.2 Slow path — tokens for local variables

`(valid, v = data) |=> ##[1:8] (out == v)`: attempts at the same position with
different captured values are not interchangeable, and the value space is too
large to tabulate. These assertions run as a set of tokens
(NFA state, captured values); identical tokens are merged. Cost grows with the
number of outstanding attempts, but only for these assertions and only while
something is outstanding — while waiting for the antecedent they cost the same
as fast-path assertions. Local variables are in v1.

**Symbolic stepping (phase 3, `_symenv.py`).** An attempt is kept as a
*shape* plus values: the shape is its term with each distinct env (tuple of
local values) replaced by a slot number, the env's rank in the sorted list of
the term's distinct envs; the values are that list. The split is canonical:
equal terms give equal pairs, so attempts merge exactly as before. Shapes are
as finite as env-free terms, so transitions are cached per (shape, base-atom
inputs), as on the fast path. The env-dependent part of a transition is a
decision tree over the conditions that read locals, built lazily: a missing
branch is filled by stepping the template symbolically again, with the known
answers along the path and the others evaluated concretely. A leaf holds the
verdict, the next shape and generated code that computes the next envs
(assignments); a final renumbering keeps the pair canonical. The template
semantics stay defined once (the templates run on symbolic envs).
Local-variable assertions also skip quiet ticks like fast-path ones, when the
idle outcome reads no local. `COCOTBEXT_SVA_SYMBOLIC=0` steps concrete terms
(used by the differential test).

### 5.3 `cover` counting

SVA counts every matching attempt; merging loses multiplicity. Cover properties
keep a count per state (no full merging), so hit counts match SVA.

### 5.4 Demand-driven reads

Each DFA state knows which atomic conditions its outgoing transitions depend
on. Each cycle the sampling loop reads only the union of signals required by the
current states of all assertions on that clocking event. An idle
`req |-> ##[1:3] ack` reads only `req`.

Always-read signals: operands of `$past` / `$rose` / `$fell` / `$stable` /
`$changed` (their history must be continuous), kept in ring buffers sized by the
largest `$past` depth. `disable iff` operands are read only when the assertion
is not idle.

### 5.5 Clock loop and idle sleep (phase 2)

Phase 0 showed the per-cycle Python wake (3–7 µs) is the dominant fixed cost
once the engine itself is fast, so this moved forward from phase 3.

**Clock loop.** Each clocking domain is a callback registered on cocotb's
edge trigger of the clock and re-armed at every edge, not a task awaiting the
trigger: no task resume per tick. Trigger callbacks run before the tasks the
same edge resumes, so a test writing a signal at the edge never affects the
values sampled (a raw GPI callback does not have this guarantee: on Icarus it
ran after the test's writes). Exceptions raised in the loop (fatal severity,
user callbacks) go to a small relay task that re-raises them in the test.

**Idle sleep.** After a tick where every assertion of the domain is idle —
nothing in flight, and the next ticks would only increment counters (no
failure, no pass callback, no cover hit) — and every `$past` history is
steady (all entries equal, and equal to what that tick read), the clock
callback only counts edges. Value-change
callbacks on every signal the idle states read (plus history operands and the
clocking `iff`) wake the domain; the counted edges are then applied exactly to
the attempt / vacuity / pass counters and the tick number. Python-valued
inputs (`values=`) cannot be watched and prevent sleep. Control calls
(`on`/`off`/`kill`), new assertions, `stats()` and `finalize()` wake the
domain first. Wake-up callbacks are never cancelled (cancelling value-change
callbacks from inside one hangs a commercial simulator): at most one is armed
per signal, and a stale one costs one evaluated tick. Verified by an
end-to-end test comparing a sleeping and a non-sleeping domain on the same
clock (identical failures and counters) on every simulator. Counting the
slept edges with raw GPI callbacks instead of the trigger callback was tried
in phase 3 and not adopted: it cannot be both exact and safe at the end of
the simulation on every simulator (`phase3-results.md`, 3.4).

## 6. Sampling semantics

SVA samples values in the Preponed region — before the clock edge's updates.

Measured in phase 0 on every simulator (see `phase0-results.md` §1):

- **Clock driven from cocotb** (`cocotb.clock.Clock`, GPI or Python
  implementation): reads at the edge callback return pre-edge values on every
  simulator — correct SVA sampling. `EdgeSampler` is supported for this case.
- **Clock derived inside the HDL** (e.g. a divided clock): signals of the
  source domain read post-edge on **every** simulator (delta-cycle / NBA race),
  so `EdgeSampler` is wrong there regardless of simulator.
- **Clock generated inside the HDL** (`after` / `#` delays): correct on
  event-driven simulators, post-edge on Verilator (callbacks run after `eval()`).
- The package detects assertions clocked by a signal not driven from cocotb
  and warns; correct support arrives with `PreponedSampler` (phase 4).

X/Z handling: by default an unknown value in a boolean condition is false (like
an `if`). A strict mode treats unknown in a condition as a failure.
`std_logic` `H`/`L` count as 1/0; `U X W Z -` count as unknown.

## 7. User-facing behavior

### 7.1 Reporting at failure time

Logged through cocotb's logger at the failing sample, so it carries the
simulation time and interleaves with the rest of the testbench log:

```
 160.00ns ERROR  sva.top.req_ack  FAILED  (attempt started 120.00ns, cycle 12 → 16)
   req |-> ##[1:3] ack
                   ^^^ not seen within [1:3] cycles
   sampled: req@120=1  ack@130=0 ack@140=0 ack@150=0
```

The sampled-values line comes from a small history buffer (on by default).
Full value traces of every involved signal are an opt-in debug mode, because
they defeat demand-driven reads. Optional: a marker file of failure times for
waveform viewers.

### 7.2 Severity

SV semantics: `$info` / `$warning` / `$error` are logged and collected;
`$fatal` stops the test immediately. Overridable per assertion. Custom action
callbacks (`on_fail=`, `on_pass=`) are supported.

### 7.3 Finalization

At the end of a checked region the package checks pending strong obligations
(`s_eventually`, `s_until`, …), logs a summary (attempts, real passes, vacuous
passes, failures, cover hits, never-triggered warnings) and fails the test if
any assertion failed.

- `@checked` decorator under `@cocotb.test()` — the standard form.
- `async with assertions:` — limits checking to part of a test.
- Safety net: if a test ends with uncollected failures (neither form used), a
  loud error is logged when cocotb cancels the test's tasks.

Engine tasks live per test (cocotb 2.x cancels test tasks at test end).

### 7.4 Assertion control

`on()` / `off()` / `kill()` with glob patterns on names — equivalents of
`$asserton` / `$assertoff` / `$assertkill` / `$assertcontrol`, including
pass/fail/vacuous action control.

## 8. Keyword coverage

**Immediate:** `assert`, `assume`, `cover`; deferred `#0` and `final`
(evaluated once in the ReadOnly phase of the time step, last evaluation wins).

**Sequences:** `##n`, `##[m:n]`, `##[m:$]`, `##[*]`, `##[+]`; `[*n]`, `[*m:n]`,
`[*]`, `[+]`, `[->n]`, `[=n]`; `and`, `or`, `intersect`, `within`,
`throughout`, `first_match`; `.triggered` / `.ended`.

**Properties:** `|->`, `|=>`, `#-#`, `#=#`, `not`, `and`, `or`, `if`/`else`,
`case`, `implies`, `iff`; `disable iff` (and group default); `nexttime`,
`s_nexttime`, `always`, `s_always`, `eventually`, `s_eventually`, `until`,
`s_until`, `until_with`, `s_until_with`; `accept_on`, `reject_on`,
`sync_accept_on`, `sync_reject_on`; `strong()`, `weak()`.

**Sampled-value / bit functions:** `$past(e, n, gate)`, `$rose`, `$fell`,
`$stable`, `$changed`, `$sampled`, `$onehot`, `$onehot0`, `$isunknown`,
`$countones`, `$countbits`, `$signed`, `$unsigned`.

**Directives:** `assert property`, `assume property` (as assert in simulation),
`cover property`, `cover sequence`, `expect` (`await a.expect(...)`).
Named / parameterized sequences and properties; `let` as a macro.

**Deferred:** `checker` blocks (use Python classes), `restrict`, global-clock
functions (`$past_gclk` …), recursive properties, multi-clock sequences
(phase 4).

## 9. Semantic decisions (settled in phases 1 and 2)

Settled by the golden corpus (behaviour of a mature SVA implementation) where
the LRM leaves room, otherwise by the LRM:

| Topic | Decision |
|---|---|
| Default strength | From the directive only: weak in assert/assume, strong in cover. `not` does not change it (16.12.2) |
| Vacuity | Property of the evaluation (16.14.8): failures can be vacuous too. `or`/`iff` decide once truth and vacuity are known; vacuity of a pending evaluation can be known early (an implication whose antecedent matched) |
| `implies` | Vacuous when the left side is false |
| `#-#`, `#=#` | Existential dual of implication; an antecedent that can still match at the end of the test is not a failure |
| End of test | Pending weak obligations pass silently, strong ones fail; a `|=>` consequent that has not seen a tick fails only if it is a strong obligation (`s_nexttime`, `s_always`, `s_eventually`, `s_until`, strong sequence); a negation or `#-#` there never fails |
| Strong operators | `s_nexttime`, `s_always`, `s_eventually`, `s_until(_with)` make the default strength of their operands strong |
| `|=>` with an empty antecedent match | The match ends at the start tick: the consequent starts at the next tick (and the attempt is nonvacuous). Empty matches of a `|->` antecedent are ignored |
| `always`, `eventually` | A new evaluation of the operand starts at every tick of the range. Successes are never vacuous; one vacuous success of the operand decides `eventually` |
| `until` family | P until Q: Q at some tick k, P at every tick before k (`_with`: up to and including k); weak forms also hold if P always holds. General properties as operands |
| `accept_on`, `reject_on` | Checked at clock ticks (sync and async forms alike), the first tick of the evaluation included. A true condition decides the attempt at that tick (accept: success, reject: failure) whatever the operand's verdict there, except that a vacuous success stays vacuous |
| Single-attempt assertions | `initial=True`: one attempt at the first tick; pending strong obligations are reported at the end like any other |
| Sequence `and` | Empty matches of an operand do not take part |
| Contradictions | A transition whose conditions require the same expression true and false is dropped at build time (the attempt fails one tick earlier than the formal truncated-path semantics) |
| `$past` before history | Value at the time the assertion was created (the "default sampled value") |
| `disable iff` | Evaluated at clock ticks; disabled attempts produce no verdict |
| Expressions | `a -> b` is `!a || b`, `a <-> b` is `!a == !b` |
| `cover` | Counts nonvacuous matches; non-matching attempts are not errors |

Known differences from the reference implementation are listed with reasons
in `tests/test_corpus.py`. In every one of them the engine agrees with the
reference interpreter (formal semantics plus the decisions above), or the
difference comes from contradiction pruning; see `phase2-results.md` for the
classes.

## 10. Verification strategy

1. **Reference interpreter** — a deliberately slow, literal implementation of
   the formal SVA semantics (IEEE 1800 Annex F). The fast engine is fuzzed
   against it with random properties and random traces, in-process, no
   simulator.
2. **Golden corpus** — properties and traces run through a mature commercial
   SVA implementation; the normalized results (failure times, attempt start
   times, pass / vacuous counts, cover hits, pending strong obligations) are
   committed as plain files. Public CI replays the corpus through the engine
   with no commercial tool. When engine and reference interpreter agree but the
   corpus disagrees, our reading of the LRM is re-checked; the LRM is the
   authority and deviations are documented. Committed files contain no tool
   names or version strings.
3. **Cross-simulator trace player** — one DUT in VHDL and one in Verilog
   replays traces; the same cocotb test runs on every simulator. Differences
   between simulators point at sampling / access bugs, not engine bugs.
4. **Native oracles** — Verilator's SVA support and GHDL's PSL support cross-
   check the subset they implement.
5. **Calibration tests** per simulator: pre/post-edge reads, enum and record
   access, hierarchical access, read cost.

Discrepancies found by fuzzing are shrunk automatically to a minimal property +
trace and added to the regression suite.

## 11. Performance strategy

| Tier | Technique | Phase |
|---|---|---|
| 1 | One sampling loop per clocking event; lazy DFA; each signal read at most once per cycle; demand-driven reads | 1 |
| 2 | Fast read path: low-level GPI reads instead of `.value` (5–10× cheaper for narrow signals). Reader chosen per (GPI interface, object type) from a calibrated table, never by probing a live value: VPI ≤ 32 bits → GPI long (masked to unsigned); VHPI any width and anything > 32 bits → GPI binary string. Private cocotb API isolated in one adapter module with a public-API fallback and a per-simulator self-test in CI | 1 |
| 3 | Idle sleep | 2 |
| 4 | Optional compiled kernel (Rust/PyO3, abi3 wheels, separate distribution) with pure-Python fallback — built in phase 3 because the pure-Python busy cost stayed above target (see `phase3-results.md`) | 3 |
| 5 | `TraceSampler` offline mode on FST/VCD dumps (zero simulation overhead) | 3 |
| 6 | `ProbeSampler`: generated HDL wrapper (VHDL-2008 external names / Verilog hierarchical references), one packed read per cycle, optional HDL-side batching (reports delayed by ≤ N cycles, failure time still exact) | 4 |

Targets are absolute overhead per simulated cycle, because slowdown ratios
depend on DUT size (phase 0 results §3):

| Item | Target |
|---|---|
| Wake per clocking event per cycle | the GPI floor |
| Fast-path assertion, active, per cycle (pure Python) | ≤ 0.3 µs |
| Fast-path assertion, idle, per cycle | ≤ 0.1 µs |
| All assertions idle, with idle sleep | no per-cycle wake |
| Optional compiled kernel, per assertion per cycle | ≤ 0.05 µs |

For reference, a coroutine-per-attempt implementation measured ~8.6 µs per
assertion per cycle.

## 12. Packaging and development

- Distribution `cocotbext-sva`, import `cocotbext.sva`, BSD-3-Clause.
- Pure-Python wheel; only runtime dependency is cocotb 2.x.
- Optional compiled kernel: separate distribution `cocotbext-sva-kernel`
  (`kernel/`, Rust + PyO3, abi3 wheels built with maturin), import
  `cocotbext_sva_kernel`, pulled in by `cocotbext-sva[fast]`. Used
  automatically when importable; `COCOTBEXT_SVA_KERNEL=python|native|auto`.
  It holds the per-tick state of fast-path assertions (transition tables,
  attempts in flight, counters); the transition semantics stay in Python
  (the kernel calls back on a cache miss). Both engines run the full test
  suite, and a differential test requires identical results.
- `nox` is the single task runner on Windows, WSL/Linux and CI.
- Simulator matrix: nvc, GHDL, Verilator, Icarus on Linux; nvc on Windows.
  Other cocotb-supported simulators are expected to work through the same
  cocotb interfaces.

## 13. Phases

0. **Calibration and benchmarks** — done, see `phase0-results.md`.
   Phase 1 — done, see `phase1-results.md`.
   Phase 2 — done, see `phase2-results.md`.
   Phase 3 — done, see `phase3-plan.md` and `phase3-results.md`.
1. **Core:** parser + IR, NFA + lazy DFA, `EdgeSampler` with demand-driven
   reads, immediate + deferred assertions, sampled-value functions, all
   sequence operators, implications, `disable iff`, clocking events with `iff`,
   local variables, reporting + finalization, reference interpreter + fuzzing,
   golden-corpus harness.
2. **Full property language:** `always` / `eventually` / `until` /
   `nexttime`, `accept_on` / `reject_on`, `cover`, `expect`, assertion control,
   statistics, vacuity warnings; idle sleep.
3. **Speed:** optional compiled kernel, offline `TraceSampler`.
4. **Clocking extensions:** `PreponedSampler`, multi-clock sequences,
   `ProbeSampler`, library of ready-made checkers.

## Appendix A — API sketch (not implemented)

```python
import cocotb
from cocotb.clock import Clock
from cocotbext.sva import Assertions, Checker, Property, checked, posedge

VALID_HELD = Property("valid && !ready |=> valid && $stable(data)",
                      args=("valid", "ready", "data"))

class StreamChecker(Checker):                  # like an SV checker / bind module
    ports = ("clk", "rst_n", "valid", "ready", "data")
    clock = "posedge clk"
    disable_iff = "!rst_n"

    def build(self, a):
        a.assert_property(VALID_HELD(valid="valid", ready="ready", data="data"),
                          name="valid_held")
        a.assert_property("!$isunknown(valid)", name="valid_known")
        a.cover_property("(valid && !ready)[*3]", name="backpressure_3")

def my_assertions(dut, model):
    a = Assertions(scope=dut, clock=posedge(dut.clk), disable_iff="!rst_n",
                   name="top", params={"DEPTH": 16})
    a.assert_property("req |-> ##[1:3] ack", name="req_gets_ack")
    a.assert_property("u_ctrl.state == BUSY && done |=> u_ctrl.state == IDLE",
                      name="fsm_returns_idle")              # VHDL enum literals
    a.assert_property("@(posedge clk) disable iff (!rst_n) "
                      "$rose(start) |=> busy until_with done", name="busy_until_done")
    a.assert_property("go |=> ##[0:15] fin", name="alu_latency",
                      signals={"go": dut.u_core.u_alu.op_start_i,
                               "fin": dut.u_core.u_alu.op_done_o})
    a.assert_property("out_valid && out_ready |-> out_data == expected",
                      name="matches_model", values={"expected": model.peek_next})
    a.assert_property("(req && ack, t = req_tag) |-> ##[1:8] (resp && resp_tag == t)",
                      name="resp_tag_matches")              # local variable
    a.sequence("burst4", "(in_valid && in_ready)[*4]")
    a.cover_property("burst4 ##1 !in_valid", name="burst_then_idle")
    a.assert_property("full |-> !in_ready", name="no_accept_when_full",
                      severity="fatal",
                      message="accepted data while full (count={u_ctrl.count})",
                      on_fail=lambda failure: model.dump_debug(failure))
    a.assert_property("psel && !penable |=> psel && penable", name="apb_access",
                      clock=posedge(dut.pclk), disable_iff="!presetn")
    a.assert_property("busy |-> !start", name="no_restart",
                      clock=posedge(dut.clk).iff("clk_en"))
    StreamChecker(a, name="in", clk=dut.clk, rst_n=dut.rst_n,
                  valid=dut.in_valid, ready=dut.in_ready, data=dut.in_data)
    for i in range(4):
        StreamChecker(a, name=f"lane{i}", scope=dut.g_lane[i])
    return a

@cocotb.test()
@checked                                    # collect failures, fail at test end
async def test_traffic(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    a = my_assertions(dut, model := RefModel())
    a.off("top.matches_model")
    await reset(dut)
    a.on("top.matches_model")
    a.check("u_ctrl.count == 0", msg="counter not cleared by reset")   # immediate
    a.check("!(full && empty)", deferred=True)                          # assert #0
    await drive_traffic(dut, model, n=10_000)
    ok = await a.expect("##[1:20] done", name="start_completes")

@cocotb.test()
async def test_after_config(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await configure(dut)
    async with my_assertions(dut, RefModel()):  # checked only inside the block
        await drive_traffic(dut, RefModel(), n=1_000)
```
