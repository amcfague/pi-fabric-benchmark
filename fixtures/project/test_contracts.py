"""Failure fixture used by the debug and integration workloads."""

import unittest

import billing
import catalog
import shipping


class ContractTests(unittest.TestCase):
    def test_available_units_subtracts_reservations(self):
        self.assertEqual(catalog.available_units(11, 4), 7)

    def test_subtotal_multiplies_unit_cents(self):
        self.assertEqual(billing.subtotal_cents(1250, 4), 5000)

    def test_free_shipping_includes_threshold(self):
        self.assertEqual(shipping.shipping_fee_cents(5000), 0)


if __name__ == "__main__":
    unittest.main()
