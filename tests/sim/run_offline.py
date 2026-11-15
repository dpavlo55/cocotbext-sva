"""Offline vs online on the simulator given by SIM (and TOPLEVEL_LANG): the
assertions of test_offline_sim.py run online while the simulator writes a VCD
dump, then TraceSampler checks the dump; failures (times, start times, counts)
and counters must be identical."""
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parents[1]))
sys.path.insert(0, str(here.parents[1] / "src"))
from devtools.simrun import run, selected_lang, selected_sim  # noqa: E402

sim = selected_sim()
lang = selected_lang(sim, [here / "dut.vhd"], [here / "dut.sv"])
out = here / "sim_build" / f"offline_{sim}_{lang}"
out.mkdir(parents=True, exist_ok=True)
vcd, result = out / "dump.vcd", out / "online.json"
for f in (vcd, result):
    f.unlink(missing_ok=True)
os.environ["WAVE_VCD"] = str(vcd)
run(here=here, toplevel="dut", test_module="test_offline_sim", tag="offline",
    vhdl=[here / "dut.vhd"], verilog=[here / "dut.sv"],
    extra_env={"OFFLINE_RESULT": str(result)})

from cocotbext.sva import TraceSampler  # noqa: E402
from cocotbext.sva._offline import VcdReader  # noqa: E402
from test_offline_sim import CLOCK, DISABLE, PROPS  # noqa: E402

online = json.loads(result.read_text())
# the DUT's scope in the dump (simulators add levels, e.g. TOP.dut)
reader = VcdReader(vcd)
scope = min((n[:-4] for n in reader.vars if n.lower().endswith(".req")), key=len)
reader.close()
ts = TraceSampler(vcd, clock=CLOCK, scope=scope, disable_iff=DISABLE)
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
print(f"offline check on {sim}/{lang}: {res.cycles} cycles, {len(res.failures)} failure reports, "
      f"{sum(st.get('passes', st.get('hits', 0)) for st in res.stats.values())} passes/hits")
if problems:
    sys.exit("offline results differ from online:\n  " + "\n  ".join(problems))
print("offline results identical to online")
