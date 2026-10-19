05_partial_fills_sum_to_cum

hbt #316 class. Shadow bid 30 @ 5000.00 behind 100 lots, o3 (100) behind it. Three trades fill it 10, 15 and 5: cum_qty 10, 25, 30, leaves 20, 5, 0, end state filled. A simulator that reports cumulative quantities as per-fill quantities (or double counts partials) shows a filled quantity above 30.
