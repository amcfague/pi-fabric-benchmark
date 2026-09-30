# Same-file integration contention

Fix the inventory, subtotal, and free-shipping defects. Add `grand_total_cents` to `shipping.quote_order`; preserve all existing return keys.

Delegate four independent tasks: inventory, subtotal, `shipping_fee_cents`, and `quote_order`. The last two edit different functions in the same `shipping.py` file. Their sizes are deliberately uneven. The benchmark records both edit attempts and grades the final combined file; a lost edit is a stress-test failure, not a main-matrix result.
