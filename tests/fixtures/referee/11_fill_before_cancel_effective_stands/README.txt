11_fill_before_cancel_effective_stands

Latency race. Shadow bid 10 @ 5000.00 with 5 ms latency (live at +5 ms, activated at +10 ms) sends a cancel at +20 ms, effective at +25 ms. A sweep at +22 ms trades through its queue (110 at 5000.00 against o1, o2 and o3 behind it): the fill happens before the cancel is effective and stands (filled 10). A simulator that applies the cancel at its send time reports it cancelled and unfilled.
