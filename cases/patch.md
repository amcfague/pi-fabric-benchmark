# Repair the checkout defects

Fix the one behavior defect in each module while preserving the public function names, signatures, and return shapes. Keep the fix limited to `catalog.py`, `billing.py`, and `shipping.py`.

The contracts are in the function docstrings. Ensure inventory reservations reduce available stock and the result never goes below zero; subtotal uses integer cents multiplied by quantity; free shipping applies at and above the threshold. The composed `shipping.quote_order` result must agree with all three module contracts for 11 stock, 4 reserved, unit price 1250 cents, and quantity 4.

Do not edit benchmark cases, expected answers, or verifier files. Return a brief summary of the three changes after the implementation.
