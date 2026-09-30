# One-file control

Inspect `billing.subtotal_cents` without editing the fixture. For unit price `1250` cents and quantity `4`, report the value produced by the current implementation and the contract-correct value (unit price multiplied by quantity).

Return only a JSON object with numeric `observed_cents` and `required_cents` fields.