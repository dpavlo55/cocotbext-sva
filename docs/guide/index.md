# User guide

The details of writing and running assertions. New to the package? Start
with [Getting started](../getting-started.md). Differences from the reference
implementation, simulator-specific behaviour and measurements are in the
[notes](../notes.md).

[Writing assertions](assertions.md)
: Assertion groups, clocks and names; reusable properties, sequences and
  checkers; immediate assertions; single attempts and `expect`; control,
  severity and results.

[Several clocks in one property](multiclock.md)
: Clock crossings and multi-clock sequences and properties.

[Sampling inside the simulator (`Probe`)](probe.md)
: A generated HDL module that samples the signals in the simulator, for the
  lowest overhead.

[Offline checking of a waveform dump](offline.md)
: The same assertions on a VCD or FST file after the simulation.

[Supported SVA](supported-sva.md)
: The sequence and property operators, functions and directives you can use.

[Semantics notes](semantics.md)
: Sampling, X/Z, `disable iff`, strength, vacuity and other fine points.

[Speed and the compiled kernel](speed.md)
: What an assertion costs per clock cycle, with and without the kernel.

[How the engine works](how-it-works.md)
: Attempts, how they are merged into one state machine, and when values are
  sampled, with diagrams.

```{toctree}
:hidden:

assertions
multiclock
probe
offline
supported-sva
semantics
speed
how-it-works
```
