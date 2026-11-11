import unittest
from datetime import date
from decimal import Decimal as D
import solution as b


class T(unittest.TestCase):
    def test_import_and_independent_notes(self):
        i1 = b.Invoice("A", "NL"); i2 = b.Invoice("B", "NL")
        i1.notes.append("x")
        self.assertEqual(i2.notes, [])

    def test_rounding(self):
        inv = b.Invoice("A", "NL", issued=date(2026, 1, 1))
        inv.add(b.LineItem("s", D("0.50")))
        self.assertEqual(inv.tax(), D("0.11"))
        self.assertEqual(inv.total(), D("0.61"))
        inv2 = b.Invoice("A", "NL", issued=date(2026, 1, 1))
        inv2.add(b.LineItem("p", D("10.25")))
        self.assertEqual(inv2.tax(), D("2.15")); self.assertEqual(inv2.total(), D("12.40"))
        inv3 = b.Invoice("A", "US")
        inv3.add(b.LineItem("x", D("9.99"), 3, D("12.5")))   # 29.97 * 0.875 = 26.22375 -> 26.22
        inv3.add(b.LineItem("y", D("0.333"), 3))            # 0.999 -> 1.00
        self.assertEqual(inv3.items[0].total(), D("26.22"))
        self.assertEqual(inv3.subtotal(), D("27.22"))
        self.assertEqual(inv3.total(), D("27.22"))

    def test_empty_invoice(self):
        inv = b.Invoice("A", "DE")
        self.assertEqual(inv.total(), D("0.00"))

    def test_add_months(self):
        self.assertEqual(b.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(b.add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(b.add_months(date(2026, 1, 31), 3), date(2026, 4, 30))
        self.assertEqual(b.add_months(date(2026, 11, 15), 2), date(2027, 1, 15))
        self.assertEqual(b.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(b.add_months(date(2026, 5, 10), 0), date(2026, 5, 10))

    def test_paginate(self):
        xs = list(range(45))
        self.assertEqual(b.paginate(xs, 1), list(range(20)))
        self.assertEqual(b.paginate(xs, 3), [40, 41, 42, 43, 44])
        self.assertEqual(b.paginate(xs, 4), [])
        with self.assertRaises(ValueError):
            b.paginate(xs, 0)

    def test_find_customer(self):
        a = b.Invoice("ACME Corp", "NL", issued=date(2026, 1, 1))
        c = b.Invoice("Acme Labs", "NL", issued=date(2026, 3, 1))
        d = b.Invoice("Other", "NL", issued=date(2026, 2, 1))
        r = b.find_customer([a, d, c], "acme")
        self.assertEqual([x.customer for x in r], ["Acme Labs", "ACME Corp"])
        self.assertEqual(b.find_customer([a], "CORP"), [a])

    def test_merge(self):
        inv = b.Invoice("A", "US")
        inv.add(b.LineItem("s", D("10"), 1, D("10")))
        inv.add(b.LineItem("s", D("10"), 1, D("0")))
        inv.add(b.LineItem("s", D("10"), 2, D("10")))
        self.assertEqual(len(inv.items), 2)
        self.assertEqual(inv.items[0].quantity, 3)
        self.assertEqual(inv.subtotal(), D("37.00"))

    def test_unchanged_behaviour(self):
        self.assertEqual(b.prorate(D("31"), date(2026, 1, 11), date(2026, 3, 1), date(2026, 1, 1), date(2026, 2, 1)), D("21.00"))
        self.assertEqual(b.prorate(D("10"), date(2026, 3, 1), date(2026, 4, 1), date(2026, 1, 1), date(2026, 2, 1)), D("0.00"))
        inv = b.Invoice("A", "AU", issued=date(2026, 1, 30))
        self.assertEqual(inv.due_date(), date(2026, 3, 1))
        self.assertEqual(b.money("2.345"), D("2.35"))
        self.assertEqual(b.Invoice("A", "XX").tax(), D("0.00"))


if __name__ == "__main__":
    unittest.main()
