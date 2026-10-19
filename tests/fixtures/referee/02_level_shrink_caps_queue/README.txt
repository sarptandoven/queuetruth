02_level_shrink_caps_queue

NT #5016 class. Shadow bid 10 @ 5000.00 has 100 lots ahead (o1 60, o2 40) and o3 (30) behind. o1 cancels 60, so the displayed level (70) falls below the old queue ahead (100) and the true queue ahead is 40. A trade of 45 then fills o2 (40) and o3 (5): the exact engine fills the shadow 5 (queue depleted). A simulator that does not shrink or cap its queue estimate when the level shrinks keeps 100 ahead and never fills.
