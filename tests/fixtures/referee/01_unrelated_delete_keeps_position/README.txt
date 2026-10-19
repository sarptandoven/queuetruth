01_unrelated_delete_keeps_position

NT #4370 class. Shadow bid 10 @ 5000.00 joins behind 100 lots (o1 60, o2 40); o3 (30) joins behind it and is then deleted. Deleting an order behind the shadow must not move the shadow to the front: the following executions of 20 + 40 + 35 against o1/o2 leave 5 lots ahead, so the exact engine never fills it. A simulator that resets the queue position on any delete at the level fills the shadow at the first execution.
