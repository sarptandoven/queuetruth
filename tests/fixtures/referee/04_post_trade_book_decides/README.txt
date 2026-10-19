04_post_trade_book_decides

NT #3596 class. Bid o1 20 @ 5000.00, asks 10 @ 5000.25 and 500 @ 5000.50; shadow bid 30 @ 5000.00 is behind o1. A seller takes o1 (20) and rests its remainder 15 @ 5000.00 in the same event. The fill decision uses the post-trade book: the best ask is now at the shadow's price, so the exact engine fills the resting shadow 15 at 5000.00 as the maker (crossed, audit ruling A5) and it keeps resting with 15. A simulator that decides on the pre-trade book sees no cross and no queue fill.
