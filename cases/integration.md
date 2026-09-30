# Cross-module integration repair

Fix the inventory, subtotal, and free-shipping defects. Add `grand_total_cents` to `shipping.quote_order`; it must equal subtotal plus shipping fee, and all existing return keys must remain.

The work has three independent ownership areas: inventory, subtotal, and one shipping owner for both `shipping_fee_cents` and `quote_order`. Delegated arms assign one child to each area; stock Pi may work directly. The benchmark verifies the combined file and the below-threshold grand total afterward.