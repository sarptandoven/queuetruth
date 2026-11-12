"""Summarize the harness's output and, where present, compare it with the hftbacktest runs.

Usage: summarize.py <harness_out_dir> [<hbt_out_dir>]

Prints the 60 s fill rate of each mode, the fills only the corrected mode (the exact replay) makes within 60 s by
cause, how the faithful mode fills those orders later, and the orders where hftbacktest's first fill differs from
the faithful mode's (`<hbt_out_dir>/<symbol>.json`, from `hbt_l3_run.py`).
"""

import collections
import json
import sys
from pathlib import Path

S = 10**9
out = Path(sys.argv[1])
hbt = Path(sys.argv[2]) if len(sys.argv) > 2 else None


def within(x, s, h=60 * S):
    return x is not None and x[0] <= min(s["ts_live"] + h, s["deadline"])


rows, differ, compared = [], 0, 0
for p in sorted(out.glob("*.jsonl")):
    sym = [json.loads(line) for line in p.open()]
    rows += sym
    if hbt is not None and (hbt / f"{p.stem}.json").exists():
        h = json.loads((hbt / f"{p.stem}.json").read_text())["first_fill"]
        for s in sym:
            rep = s["faithful"][0] if s["faithful"] and s["faithful"][0] <= s["deadline"] else None
            real = h[str(s["id"])]
            real = real if real is not None and real <= s["deadline"] else None
            compared += 1
            differ += rep != real
n = len(rows)
fa = sum(within(s["faithful"], s) for s in rows)
co = sum(within(s["corrected"], s) for s in rows)
print(f"orders {n}; filled within 60 s: exact {co / n:.2%}, faithful {fa / n:.2%} ({100 * (fa - co) / n:+.2f} pp)")
miss = [s for s in rows if within(s["corrected"], s) and not within(s["faithful"], s)]
for k, v in collections.Counter(s["cause"] for s in miss).most_common():
    print(f"  missed: {k:<55} {v:7d} {v / len(miss):6.1%}")
later = collections.Counter(s["faithful"][1] for s in miss if s["faithful"])
print(f"  faithful fills {sum(later.values())} of the {len(miss)} later ({dict(later)})")
only = [s for s in rows if within(s["faithful"], s) and not within(s["corrected"], s)]
extra = collections.Counter(s["faithful"][1] for s in only)
print(f"  faithful only: {dict(extra)}")
if hbt is not None:
    print(f"hftbacktest vs faithful replica: {differ} of {compared} orders with a different first fill")
