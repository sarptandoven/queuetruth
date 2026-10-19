08_price_change_loses_priority

Shadow bid 10 @ 5000.00 behind o1 (60) and o2 (40); o3 (30) behind. o1 moves to 4999.75 and back to 5000.00, which puts it behind the shadow and o3. A trade of 45 fills o2 (40) and o3 (5): the exact engine fills the shadow 5. A simulator that treats a price modify as in place keeps o1 ahead and does not fill.
