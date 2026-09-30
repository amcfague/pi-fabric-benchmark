# Same-file integration repair

Fix the inventory, subtotal, and free-shipping defects. Also add `grand_total_cents` to `shipping.quote_order`; it must equal subtotal plus shipping fee, and all existing return keys must remain.

Delegate four independent tasks: inventory, subtotal, the `shipping_fee_cents` function, and the `quote_order` function. The last two tasks edit different functions in the same `shipping.py` file. Their sizes are intentionally uneven: the threshold fix is small; the checkout-total change is integration-sensitive. After all children finish, inspect and reconcile the combined file so neither change is lost. Run `python3 -m unittest test_contracts` and verify the grand total includes shipping below the free-shipping threshold.