09_hidden_print_no_queue

Nasdaq. Shadow bid 100 @ 100.00 behind 500 displayed shares (o1 300, o2 200). A hidden-order print of 600 shares at 100.00 (side N, no F/C) consumes no displayed queue; a displayed execution of 100 against o1 leaves 400 ahead, so the exact engine does not fill. A simulator that counts hidden prints as queue consumption fills the shadow.
