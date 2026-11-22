# Several clocks in one property


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
