"""Offline vs online on the simulator given by SIM (and TOPLEVEL_LANG): the
assertions of test_offline_sim.py run online while the simulator writes a
dump, then TraceSampler checks the dump; failures (times, start times,
counts) and counters must be identical. Done with a VCD dump, and with an FST
dump too when the simulator writes FST and the compiled kernel can read it."""
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
lang = selected_lang(sim, [here / "dut.vhd"], [here / "dut.sv"])
try:
    import cocotbext_sva_kernel as _k
    fst_readable = hasattr(_k, "FstFile")
except ImportError:
    fst_readable = False
formats = ["vcd"] + (["fst"] if sim in FST_SIMS and fst_readable else [])
if sim in FST_SIMS and not fst_readable:
    print("FST not checked: the compiled kernel with the FST reader is not installed")


def check(fmt):
    out = here / "sim_build" / f"offline_{sim}_{lang}"
    out.mkdir(parents=True, exist_ok=True)
    dump, result = out / f"dump.{fmt}", out / f"online_{fmt}.json"
    for f in (dump, result):
        f.unlink(missing_ok=True)
    os.environ.pop("WAVE_VCD", None)
    os.environ.pop("WAVE_FST", None)
    os.environ[f"WAVE_{fmt.upper()}"] = str(dump)
    run(here=here, toplevel="dut", test_module="test_offline_sim", tag=f"offline_{fmt}",
        vhdl=[here / "dut.vhd"], verilog=[here / "dut.sv"],
        extra_env={"OFFLINE_RESULT": str(result)})

    from test_offline_sim import CLOCK, DISABLE, PROPS

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
    res = ts.run()
    offline = {
        "failures": sorted([f.assertion, f.time_ns, f.start_times_ns, f.count, f.at_end]
                           for f in res.failures),
        "stats": res.stats,
    }
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
    print(f"offline check on {sim}/{lang} ({fmt.upper()}): {res.cycles} cycles, "
          f"{len(res.failures)} failure reports, "
          f"{sum(st.get('passes', st.get('hits', 0)) for st in res.stats.values())} passes/hits")
    if problems:
        sys.exit(f"offline results ({fmt.upper()}) differ from online:\n  " + "\n  ".join(problems))
    print(f"offline results ({fmt.upper()}) identical to online")


for fmt in formats:
    check(fmt)
