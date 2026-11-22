# cocotbext-sva-kernel

Optional compiled kernel of [cocotbext-sva](https://github.com/dpavlo55/cocotbext-sva)
(SystemVerilog-style assertions for cocotb), and the FST reader of its
offline checker.

Install it with the package:

```bash
pip install cocotbext-sva[fast]
```

It is used automatically when installed and gives the same results as the
pure-Python engine, faster. `COCOTBEXT_SVA_KERNEL=python` turns it off;
`native` requires it. It is not meant to be used on its own.
