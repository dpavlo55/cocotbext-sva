"""Print the reader-calibration table."""
import json
from pathlib import Path

p = Path(__file__).parent / "types.jsonl"
for l in p.read_text().splitlines():
    r = json.loads(l)
    print(f"{r['sim']:9} {r['lang']:8} {r['sig']:6} {r['cls']:17} {r['type']:16} rng={r['range']} "
          f"sg={r['signed']} val={r['value']!s:12.12} bin={r['get_signal_val_binstr']!s:12.12} "
          f"long={r['get_signal_val_long']}")
