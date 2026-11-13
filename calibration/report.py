"""Print calibration results as a table (one row per simulator/language/case)."""
import json
from pathlib import Path

rows = [json.loads(l) for l in (Path(__file__).parent / "results.jsonl").read_text().splitlines() if l]
for r in rows:
    probes = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("sim", "lang", "case"))
    print(f"{r['sim']:<10} {r['lang']:<8} {r['case']:<20} {probes}")
