15_cme_same_size_modify_loses_priority

CME, audit ruling A6. Shadow bid 10 @ 5000.00 behind o1 (60) and o2 (40); o3 (30) behind. o1 sends a modify with the same price and size (iceberg refresh / attribute change), which loses priority on CME. A trade of 45 fills o2 (40) and o3 (5): the exact engine fills the shadow 5. A simulator that keeps priority on any non-increasing modify keeps 100 ahead and does not fill.
