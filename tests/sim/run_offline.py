"""Offline vs online on the simulator given by SIM (and TOPLEVEL_LANG): the
assertions of test_offline_sim.py run online while the simulator writes a
dump, then TraceSampler checks the dump; failures (times, start times,
counts) and counters must be identical. Done with a VCD dump, and with an FST
dump too when the simulator writes FST and the compiled kernel can read it.
Then the same with the assertions of test_offline_clocks.py on the design
`clocks` (clocks derived and generated in the HDL, multi-clock properties)."""
import importlib
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parents[1]))
sys.path.insert(0, str(here.parents[1] / "src"))
from devtools.simrun import FST_SIMS, run, selected_lang, selected_sim  # noqa: E402

from cocotbext.sva import TraceSampler  # noqa: E402
from cocotbext.sva._offline import open_dump  # noqa: E402

sim = selected_sim()
DESIGNS = [  # (toplevel, online test module, timing)
    ("dut", "test_offline_sim", False),
    ("clocks", "test_offline_clocks", True),
]
try:
    import cocotbext_sva_kernel as _k
    fst_readable = hasattr(_k, "FstFile")
except ImportError:
    fst_readable = False
formats = ["vcd"] + (["fst"] if sim in FST_SIMS and fst_readable else [])
if sim in FST_SIMS and not fst_readable:
    print("FST not checked: the compiled kernel with the FST reader is not installed")


def check(fmt, toplevel, module, timing):
    lang = selected_lang(sim, [here / f"{toplevel}.vhd"], [here / f"{toplevel}.sv"])
    out = here / "sim_build" / f"offline_{toplevel}_{sim}_{lang}"
    out.mkdir(parents=True, exist_ok=True)
    dump, result = out / f"dump.{fmt}", out / f"online_{fmt}.json"
    for f in (dump, result):
        f.unlink(missing_ok=True)
    os.environ.pop("WAVE_VCD", None)
    os.environ.pop("WAVE_FST", None)
    os.environ[f"WAVE_{fmt.upper()}"] = str(dump)
    run(here=here, toplevel=toplevel, test_module=module, tag=f"offline_{toplevel}_{fmt}",
        vhdl=[here / f"{toplevel}.vhd"], verilog=[here / f"{toplevel}.sv"], timing=timing,
        extra_env={"OFFLINE_RESULT": str(result)})

    mod = importlib.import_module(module)
    CLOCK, PROPS, DISABLE = mod.CLOCK, mod.PROPS, getattr(mod, "DISABLE", None)

    online = json.loads(result.read_text())
    # the DUT's scope in the dump (simulators add levels, e.g. TOP.dut; GHDL's
    # FST puts the top-level signals at the root)
    reader = open_dump(dump)
    scope = min((n[:-3].rstrip(".") for n in reader.vars
                 if n.lower() == "req" or n.lower().endswith(".req")), key=len)
    reader.close()
    ts = TraceSampler(dump, clock=CLOCK, scope=scope, disable_iff=DISABLE)
    for name, (kind, text) in PROPS.items():
        (ts.cover_property if kind == "cover" else ts.assert_property)(text, name=name)
    res = ts.run(until_ns=online["end"])     # (GHDL dumps one more time step)
    offline = {
        "failures": sorted([f.assertion, f.time_ns, f.start_times_ns, f.count, f.at_end]
                           for f in res.failures),
        "stats": res.stats,
    }
    # (the time of a failure at the end of the test is not that of a tick)
    for fails in (offline["failures"], online["failures"]):
        for f in fails:
            if f[4]:
                f[1] = None
        fails.sort(key=json.dumps)
    problems = []
    if offline["failures"] != online["failures"]:
        on = {json.dumps(x) for x in online["failures"]}
        off = {json.dumps(x) for x in offline["failures"]}
        problems.append(f"failures: online only {sorted(on - off)[:5]}, offline only {sorted(off - on)[:5]}")
    for name in PROPS:
        a, b = online["stats"][name], offline["stats"][name]
        common = {k: a[k] for k in a if k in b}
        if common != {k: b[k] for k in common}:
            problems.append(f"{name}: online {a} offline {b}")
    what = f"{toplevel} on {sim}/{lang} ({fmt.upper()})"
    print(f"offline check of {what}: {res.cycles} cycles, "
          f"{len(res.failures)} failure reports, "
          f"{sum(st.get('passes', st.get('hits', 0)) for st in res.stats.values())} passes/hits")
    if problems:
        sys.exit(f"offline results of {what} differ from online:\n  " + "\n  ".join(problems))
    print(f"offline results of {what} identical to online")


for design in DESIGNS:
    for fmt in formats:
        check(fmt, *design)
