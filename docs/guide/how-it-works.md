# How the engine works

You do not need this page to use the package. It explains, without jargon
where possible, what happens between writing `a.assert_property("req |->
##[1:3] ack")` and seeing a failure in the log, and why that stays fast with
many assertions running at once.

## Compile once, then one small step per clock tick

```{image} img/pipeline.svg
:alt: The property goes through a parser and a compiler once, giving an automaton. At every clock tick a sampler reads the signals, the engine steps the automaton, and a reporter logs the results.
:width: 100%
```

The work is split in two.

**Once, when the assertion is created.** The property text is parsed into a
syntax tree, and every name in it is looked up in the design: a typo fails
here, with suggestions, before the simulation runs. A compiler then turns the
tree into an *automaton*, a state machine that knows, for every situation the
property can be in, what to do next. The conditions the property tests
(`req`, `ack`, `count == $past(count) + 1`) become small generated Python
functions.

**At every clock tick.** A *sampler* reads the signals; the *engine* moves the
automaton one step with those values; a *reporter* logs a failure the moment
it happens and keeps the counters for the summary.

The engine never talks to the simulator: it only receives values. That is
why the same engine can check a waveform dump after the simulation
([offline checking](offline.md)), and why most of its tests run without a
simulator at all.

## Attempts: a new one at every tick

```{image} img/attempts.svg
:alt: A timing diagram of req, ack and the attempts of the property. The attempt started at tick 1 passes at tick 3; the one started at tick 5 fails at tick 8; the others are vacuous.
:width: 100%
```

A concurrent assertion is not checked once: SVA starts a new *attempt* of the
property at every clock tick, and each attempt runs on its own until it
passes or fails.

For `req |-> ##[1:3] ack`, an attempt that starts while `req` is 0 has
nothing to check; it succeeds at once and is called *vacuous*. An attempt
that starts while `req` is 1 waits for `ack` in a window of 1 to 3 ticks
later. Above, the attempt from tick 1 sees `ack` at tick 3 and passes; the
attempt from tick 5 reaches the end of its window at tick 8 without `ack` and
fails. The failure is reported at tick 8, with the time the attempt started.

Several attempts can be waiting at the same time: if `req` stays 1 for a
while, every tick adds one more.

## Merging attempts

```{image} img/merging.svg
:alt: Three attempts that have waited 1, 2 and 3 ticks are merged into one state {1, 2, 3}. A table maps the current state and the inputs to the next state and the events.
:width: 100%
```

Keeping a separate record for every attempt would make the cost grow with
the number of attempts in flight. The engine avoids that with one
observation: what happens next to an attempt depends only on *where it is*
in the property, not on when it started. Two attempts that have both waited
two ticks for `ack` will behave exactly alike from now on.

So the engine does not track attempts one by one. It tracks the *set of
positions* that are occupied: `{1, 2, 3}` above means "one attempt has waited
1 tick, one 2 ticks, one 3 ticks". For the failure report it also keeps the
start time of the oldest attempt and how many there are.

Each such set is one state of a bigger state machine, and moving from one
state to the next is a row in a table: *this state, these input values →
that next state, these events*. The engine does not build the whole table in
advance (it could be huge); it works out a row the first time it is needed
and keeps it. After a short while every situation the test produces has been
seen, and each tick costs one evaluation of the conditions and one table
lookup, however many attempts are in flight.

Covers are the exception: SVA counts every matching attempt, so a cover keeps
a count per state instead of merging completely, and its hit counts match
SVA's.

## Local variables: the slower path

A property with a local variable, such as
`(valid, v = data) |=> ##[1:8] (out == v)`, cannot be merged as freely: two
attempts at the same position remember different values of `v`, and the
values are too many to put in a table. These assertions keep their attempts
as tokens (position plus remembered values), still merging the ones that are
identical, and still caching what they can. They cost more only while
attempts are waiting; while waiting for `valid` they cost the same as any
other assertion.

## Reading only what is needed

Reading a signal from the simulator costs time, so the engine reads only what
the current state needs. While `req |-> ##[1:3] ack` has no attempt waiting,
only `req` is read; `ack` is read only while an attempt is in its window.
The arguments of `$past`, `$rose` and the like are read at every tick, since
their history must have no gaps.

When every assertion on a clock is idle and the next ticks could only add
to the counters, the engine stops evaluating and only counts clock edges.
It asks the simulator to wake it when one of the signals it is waiting for
changes; it then applies the counted ticks exactly, so the counters stay
correct. A quiet clock domain costs almost nothing. (Set
`COCOTBEXT_SVA_IDLE_SLEEP=0` to turn this off.)

## When values are sampled

```{image} img/sampling.svg
:alt: Within the time step of a clock edge, the preponed snapshot at the start always sees the old value of a flip-flop; a read when the edge is seen sees the old value when cocotb drives the clock, and may see the new one when the clock comes from the HDL.
:width: 100%
```

SVA checks the values signals had *just before* the clock edge, not the ones
flip-flops take because of that edge. Getting this right from Python takes
care:

- **`sampling="edge"`** reads the values when the clock edge is seen. When
  cocotb drives the clock (`cocotb.clock.Clock`), that is still before the
  flip-flops update, on every simulator tested. For a clock generated or
  divided inside the HDL, the flip-flops may already have changed.
- **`sampling="preponed"`** takes a snapshot of the signals at the start of
  every time step, before anything in it happens, and a clock tick uses the
  snapshot of its own time step. It is right for every clock, at a higher
  cost per cycle.
- **`"auto"`**, the default, does both for the first 256 ticks of a clock and
  compares them. If they ever differ it switches to preponed; otherwise it
  keeps the cheaper edge sampling, and compares again briefly every 4096
  ticks.

A [`Probe`](probe.md) samples inside the simulator instead, as a flip-flop
would, and offline checking takes the value each signal had just before the
edge in the dump.

## Several clocks

A property with several clocks
([several clocks in one property](multiclock.md)) is rewritten, as the SVA
standard describes, into a property on one combined clock that ticks
whenever any of its clocks does, with a flag telling which clocks ticked.
The engine then runs it like any other property; attempts start only at the
ticks of the property's first clock.

## The compiled kernel

The optional compiled kernel (`pip install cocotbext-sva[fast]`) does the
stepping in compiled code instead of Python, with the same tables and the
same results. Python still computes the values of local variables and
conditions on them. `COCOTBEXT_SVA_KERNEL=python` turns the kernel off;
[Speed and the compiled kernel](speed.md) has the measurements.

## How the results are checked

The engine's results are compared with a separate, deliberately simple
interpreter that follows the formal semantics of IEEE 1800 word for word, on
many randomly generated properties and traces, and with a golden corpus of
about 1,200 results recorded from a commercial SVA simulator. The cases where
the two references disagree are listed in the [notes](../notes.md).
