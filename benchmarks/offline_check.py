"""Offline check of a dump written by a simulator while the online assertions
ran (benchmark scenario speed_5_package_50_mixed): the counters must agree.

    WAVE_VCD=/tmp/bench.vcd BENCH_CYCLES=5000 COCOTB_TEST_FILTER=speed_5_package_50_mixed \
        SIM=ghdl python benchmarks/run.py
    python benchmarks/offline_check.py /tmp/bench.vcd benchmarks/results.jsonl
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cocotbext.sva import TraceSampler  # noqa: E402

MIXED = ["req |-> ##[1:3] ack", "req |=> ##1 ack", "ack |-> $past(req, 2)",
         "$rose(req) |-> ##2 ack", "req ##1 req |-> ##1 ack ##1 ack"]

vcd, results = sys.argv[1], sys.argv[2]
online = [json.loads(ln) for ln in Path(results).read_text().splitlines()
          if '"5 cocotbext-sva x50 mixed"' in ln][-1]
ts = TraceSampler(vcd, clock="posedge clk", scope="bench")
for i in range(50):
    ts.assert_property(MIXED[i % len(MIXED)], name=f"p{i}")
t0 = time.perf_counter()
res = ts.run()
dt = time.perf_counter() - t0
passes = sum(st["passes"] for st in res.stats.values())
print(f"offline: {res.cycles} cycles in {dt:.2f} s ({dt / res.cycles * 1e6:.1f} us/cycle), "
      f"passes {passes}, failures {len(res.failures)}")
print(f"online : passes {online['passes']}, failures {online['failures']}")
