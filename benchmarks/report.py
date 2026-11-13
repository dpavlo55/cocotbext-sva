"""Print benchmark results: read cost per method/width, and speed per scenario."""
import json
import sys
from collections import defaultdict
from pathlib import Path

name = sys.argv[1] if len(sys.argv) > 1 else "results.jsonl"
rows = [json.loads(l) for l in (Path(__file__).parent / name).read_text().splitlines() if l]

by_target = defaultdict(list)
for r in rows:
    by_target[(r["sim"], r["lang"])].append(r)

for (sim, lang), rs in by_target.items():
    print(f"\n=== {sim} / {lang} ===")
    reads = [r for r in rs if r["kind"] == "read"]
    if reads:
        widths = sorted({r["width"] for r in reads})
        methods = list(dict.fromkeys(r["method"] for r in reads))
        print("read cost, ns per read".ljust(22) + "".join(f"{w:>9}b" for w in widths))
        for m in methods:
            cells = {r["width"]: r["ns_per_read"] for r in reads if r["method"] == m}
            print(m.ljust(22) + "".join(f"{cells[w]:>10.0f}" if w in cells else f"{'-':>10}" for w in widths))
    edge_reads = [r for r in rs if r["kind"] == "read_at_edge"]
    if edge_reads:
        print("read cost inside the edge callback: " + ", ".join(
            f"{r['method']} {r['width']}b {r['ns_per_read']:.0f} ns" for r in edge_reads))
    for r in rs:
        if r["kind"] == "python_speed":
            print(f"pure Python loop: {r['ns_per_iter']:.0f} ns per iteration")
    firsts = [r for r in rs if r["kind"] == "first_read"]
    if firsts:
        print("first read after each edge: " + ", ".join(
            f"{r['signal']} {r['ns_per_read']:.0f} ns" for r in firsts))
    for r in rs:
        if r["kind"] == "long_check":
            note = "matches .value" if r["matches"] else f"DIFFERS (true, raw) e.g. {r['examples']}"
            print(f"gpi long, {r['width']}b: {note}")
    speeds = [r for r in rs if r["kind"] == "speed"]
    if speeds:
        base = next((r["wall_s"] for r in speeds if r["scenario"].startswith("0 ")), None)
        print(f"\n{'scenario':<26}{'cycles/s':>12}{'slowdown':>10}{'us/cycle':>10}")
        for r in speeds:
            slow = f"{r['wall_s'] / base:.1f}x" if base else "-"
            print(f"{r['scenario']:<26}{r['cycles_per_s']:>12,}{slow:>10}{1e6 * r['wall_s'] / r['cycles']:>10.2f}")
