12_hidden_execution_at_price_after_queue

Nasdaq, audit ruling A3. Shadow bid 30 @ 100.00 behind o1 (60) and o2 (40); o4 (100) rests at 99.98. Both are executed (E messages), so no displayed interest is left ahead. A hidden-order print of 20 at 100.00 then fills the shadow 20 (displayed before hidden at a price), and a hidden print of 15 at 99.99, through the shadow's price while it was the best bid, fills the last 10 by trade-through. A simulator that ignores side-N prints leaves the shadow unfilled.
