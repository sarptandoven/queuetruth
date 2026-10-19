16_crossed_at_live_takes_opposite

CME, audit ruling A5. Asks 10 @ 5000.25 and 15 @ 5000.50; a marketable shadow bid 30 @ 5000.50 arrives. It fills as a taker against the displayed offers: 10 at 5000.25 and 15 at 5000.50 (reason crossed_at_live, one fill per price level), and the remaining 5 rest at 5000.50. A later event does not let it take the same offers twice. A simulator that fills the full 30 at its own limit price as a maker fails on quantity and price.
