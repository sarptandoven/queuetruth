14_off_price_print_not_trade_through

Nasdaq, audit ruling A4. Shadow bid 10 @ 100.00 behind o1 (60) and o2 (40). o1 is executed with price at 99.99 (printed at 99.99, F at 99.99, C at its resting 100.00). The print is below the shadow's price but it is an execution of an order ahead, not a trade-through: queue ahead drops to 40 and the shadow does not fill. A simulator that treats any print below the bid as a trade-through fills it.
