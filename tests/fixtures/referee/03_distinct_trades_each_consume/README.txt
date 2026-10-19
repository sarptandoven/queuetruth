03_distinct_trades_each_consume

NT #5017 class. Shadow bid 10 @ 5000.00 behind 100 lots. Two distinct trades of 50 print at the same price and the same timestamp in two separate events; each consumes its own volume, so the queue ahead reaches 0. A third trade of 50 (against o3 behind) fills the shadow 10. A simulator that merges identical prints counts only 100 traded by the end and never fills.
