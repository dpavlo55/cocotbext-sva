# Notes: differences, simulators, performance

## Differences from the reference implementation

The engine is checked against a golden corpus: properties and traces whose
results were recorded from a mature commercial SVA implementation
(`tests/corpus`, replayed by `tests/test_corpus.py` and
`tests/test_corpus_mc.py`). Where they disagree, the case is listed in the
test with its reason, and the engine follows the formal semantics of IEEE
1800 (a reference interpreter of it, `tests/refsem.py`, is fuzzed against
the engine).

### Single clock (45 of 832 cases)

| Cases | Difference |
|---|---|
| 11 | End of test: the engine (and the formal semantics) report a strong obligation still pending (`s_nexttime`, `s_eventually`, `s_always`, `s_until`, or a strong sequence whose `\|=>` consequent has not started); the reference tool does not for these top-level forms, although it does inside implications and for top-level strong sequences. |
| 13 | Single-attempt assertions (`initial=True`): the reference tool ends `always P` after the first evaluation of P when P is an implication or `s_eventually` (with a boolean P it evaluates every tick), and never reports pending strong obligations at the end. The engine treats them like any assertion. |
| 6 | `always [m:n] S` with a multi-tick sequence S: the reference tool's verdicts are not consistent with any reading (attempts with identical outcomes get different verdicts). Boolean and implication operands match. |
| 4 | The reference tool reports two verdicts for some attempts (duplicate pending failures at the end, or passes plus failures exceeding the attempts). Every engine verdict is one of them. |
| 2 | `if (c) P else (S \|=> Q)` with c true and P failing: the reference tool reports a vacuous success; IEEE 1800 16.12.8 makes the result P's. |
| 2 | `not (P and Q)` at the end of the test with Q held and P a pending weak obligation: the engine reports the failure the formal semantics gives, the reference tool does not. |
| 5 | Timing of contradictions: the engine prunes constant-false or contradictory conditions at compile time (one tick earlier or later than the reference tool, which also drops contradictions across different expressions), or notices one tick earlier that a sequence can no longer match. Verdicts agree. |

Also once each: `S1 within S2` with single-tick operands on the same tick
(the reference tool never matches), and the vacuity of `or` with a
`reject_on` / `if` operand (counted vacuous by the engine, nonvacuous by the
reference tool; verdict times agree).

### Several clocks (43 of 362 cases)

**Property operators directly after a clock change (39).** In
`@(c0) a |=> @(c1) nexttime b` the operator starts between two ticks of
`c1`.
- The engine evaluates such an operator from the next tick of its clock on:
  `nexttime b` checks `b` at the second tick of `c1` after the antecedent,
  `always [0:2] b` covers the first three.
- The reference tool counts from the antecedent's last tick: `nexttime b`
  checks `b` at the first tick of `c1`, like `b` alone, and `always [0:2] b`
  covers the first two. It also decides `not (b ##1 c)` at the antecedent's
  tick, before `c1` has ticked.
- Affected: `nexttime`, `always`, `eventually`, `until`, `not`, `#-#`
  directly after a clock change. Sequences, implications and `if` after a
  clock change agree exactly.
- The engine's reading is that of IEEE 1800-2017:
  - F.3.4.3.3: `S |=> Q` is `(S ##1 @(1) 1) |-> Q`: the consequent starts one
    step after the antecedent's tick, not yet at a tick of its own clock.
  - F.5.1.2: `@(c) nexttime p` is
    `!c until (c and nexttime (!c until (c and p)))`: first a tick of `c`,
    then the next one.
  - 16.12.10: a `nexttime` evaluated in a time step that is not a tick of its
    clock is first aligned to a tick; `nexttime[0]` is the alignment.
  - F.3.4.3.9: `always [0:2] p` is `nexttime[0] p and nexttime[1] p and
    nexttime[2] p`, the first three ticks.
  - F.5.1.2: `@(c) not p` is `not @(c) p`, which cannot be decided before
    `c` ticks.
- To write a property that both agree on, use a sequence for the delay:
  `a |=> @(c1) b` (first tick of `c1`), `a |=> @(c1) 1 ##1 b` (second).

The other four are differences already seen with one clock (a pending strong
sequence at the end, a sequence noticed one tick later as unable to match,
and `$past` before enough ticks: the reference returns X for signals without
an initial value, the engine 0).

Like the reference tool, the package rejects a sequence that can match empty
next to a clock change (16.13.1).

## Simulators

Tested on nvc, GHDL, Icarus and Verilator (Linux), nvc (Windows) and a
commercial simulator (VHDL and SystemVerilog).

**Sampling.** Reading signals when a clock edge is seen gives SVA sampling
for clocks driven from cocotb on every tested simulator, but not for clocks
derived or generated in the HDL: hence `sampling="preponed"` and the default
`"auto"` (README, "Semantics notes").

**Callbacks.** Preponed sampling takes the values at the start of every time
step (`NextTimeStep`). Behaviours found and worked around:

| Simulator | Behaviour |
|---|---|
| Verilator | Removing a `NextTimeStep` callback registered from inside a `NextTimeStep` callback crashes at the next time step. |
| Verilator | Ending the simulation in a time step in which a `NextTimeStep` callback was registered crashes at exit, removed or not. A plain cocotb task looping on `await NextTimeStep()` does the same. |
| GHDL | A `NextTimeStep` callback still armed at the end of the simulation crashes at exit; removing it is fine. |
| GHDL | Waiting for the read-only phase makes pending `after` delays in the design run early (a clock generated with `after` toggles at every time step). Multi-clock assertions are therefore evaluated in the read-write phase on GHDL. |

Remaining exposure: on Verilator, if the last test of a simulation ends in
the read-write or read-only phase while a preponed clock is being followed,
the simulator can crash at exit.

**Multi-clock assertions** are evaluated in the read-only phase of a time
step (read-write on GHDL), so their failure callbacks cannot write signals
(except on GHDL). A clock edge caused by a testbench write in the read-write
phase of a time step in which another clock of the property ticks is a
separate tick on GHDL.

**`Probe`** (generated HDL sampler):

| Simulator | Form |
|---|---|
| nvc | VHDL, external names |
| GHDL | VHDL with ports (`interface="ports"`): with external names in the design, GHDL exits with code 255 when cocotb's VPI library loads |
| Icarus | SystemVerilog as a second top-level module (`bind=False`, `-s sva_probe_...`): no `bind` |
| Verilator | SystemVerilog with `bind` |
| commercial, SV | second top-level module: a `bind` at file level is not elaborated unless compiled as one compilation unit |
| commercial, VHDL | external names (VHDL-2008) |

VHDL signals of an enumeration type declared inside an architecture cannot
be probed (the type is not visible); types from packages can.

**Waveform dumps** (offline checking): some simulators do not write the last
time step of a dump, so run a little past the last edge to check. GHDL writes
one more time step than the test ran when the design has a clock generated
in the HDL: pass the end of the test as `run(until_ns=...)`.

**X/Z.** Verilator is 2-state: `$isunknown` is always false there.

## Performance

Wall time per simulated clock cycle above the simulation without Python,
Linux, with the compiled kernel (the README has the summary):

| Load | nvc | Verilator |
|---|---|---|
| 50 assertions with attempts in flight, sampled at the clock edge | 9.1 µs | 10.1 µs |
| the same through a `Probe` of depth 64 | 3.9 µs | 5.0 µs |
| preponed sampling, extra cost per clock | about 11 µs | about 11 µs |

- A clock domain has a fixed cost of about 2 µs per cycle (two kernel calls,
  the signal reads); each busy assertion adds about 0.05 µs in the kernel.
- Local-variable assertions: 10 with attempts in flight cost 6 µs per cycle
  on nvc with the kernel (as many as 10 without local variables), 20 µs
  without it.
- Multi-clock assertions (engine alone, 50 assertions): 19 µs per tick busy
  and 3 µs idle with the kernel, 76 and 60 µs without it.
- While every assertion of a clock is idle, the clock is not followed at all
  (idle sleep).

`benchmarks/` has the benchmarks (`nox -s bench`, `engine_bench.py`,
`realistic_bench.py`, `multiclock_bench.py`).
