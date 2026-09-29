# Cross-module triage

Read the three modules without changing them. For each named function, report its current output for the given input and the output required by its docstring/contract:

- `catalog.available_units(11, 4)`
- `billing.subtotal_cents(1250, 4)`
- `shipping.shipping_fee_cents(5000)`

Then combine the intended behavior for an order of 4 units at 1250 cents each, with 11 units in stock and 4 reserved. `conclusion.available_units` means stock minus reservations before fulfillment; do not subtract the order quantity again. The free-shipping threshold is 5000 cents. State whether the order can be fulfilled, available inventory before fulfillment, subtotal, and shipping charge.

Return only a JSON object with this shape. The zeros and `false` are placeholders; calculate every value. Do not add prose or keys:

```json
{
  "findings": {
    "catalog": {"buggy_expression": "", "correct_expression": "", "observed_available_units": 0, "expected_available_units": 0},
    "billing": {"buggy_expression": "", "correct_expression": "", "observed_subtotal_cents": 0, "expected_subtotal_cents": 0},
    "shipping": {"buggy_expression": "", "correct_expression": "", "observed_fee_at_threshold_cents": 0, "expected_fee_at_threshold_cents": 0}
  },
  "conclusion": {
    "order_fulfillable": false,
    "available_units": 0,
    "subtotal_cents": 0,
    "shipping_fee_cents": 0
  }
}
```
