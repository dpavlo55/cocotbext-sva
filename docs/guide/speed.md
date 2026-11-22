# Speed and the compiled kernel


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
