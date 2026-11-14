"""Merge generated cases with reference results into tests/corpus/corpus.json.

Reference results are tool-neutral: per property, failure ticks with attempt
start ticks, end-of-simulation failures (start ticks), pass ticks
(nonvacuous), cover ticks, and attempt/vacuous/disabled counts.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
B = ROOT / "build" / "corpus"
meta = json.loads((B / "cases.json").read_text())
res = json.loads((B / "results.json").read_text())
out = []
for c in meta["cases"]:
    r = res.get(c["module"], {})
    ref = {}
    for name, kind in c["kinds"].items():
        e = r.get(name, {"fails": [], "end_fails": [], "passes": [], "covers": []})
        ref[name] = {
            "fails": sorted(map(tuple, e["fails"])),
            "end_fails": sorted(e["end_fails"]),
            "passes": sorted(e["passes"]),
            "covers": sorted(e["covers"]),
            "counts": e.get("counts"),
        }
    case = {"id": c["id"], "props": c["props"], "kinds": c["kinds"], "trace": c["trace"], "ref": ref}
    if c.get("once"):
        case["once"] = c["once"]
    out.append(case)
dest = ROOT / "tests" / "corpus" / "corpus.json"
if dest.exists() and not meta.get("all"):
    # incremental: keep existing cases, replace/append the newly run ones
    old = json.loads(dest.read_text())["cases"]
    new_ids = {c["id"] for c in out}
    out = [c for c in old if c["id"] not in new_ids] + out
dest.write_text(json.dumps({"rows": meta["rows"], "signals": meta["signals"], "cases": out}, indent=0))
print(f"{len(out)} cases -> {dest}")
