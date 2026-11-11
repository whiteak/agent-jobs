import unittest
from decimal import Decimal as D
import solution as m


class T(unittest.TestCase):
    def test_buy_fee_in_cost(self):
        L = m.Ledger(); L.buy("A", 10, 10, "d1", fee=5)
        self.assertEqual(L.cost_basis("A"), D("105.00"))
        self.assertEqual(L.average_cost("A"), D("10.50"))
        self.assertEqual(L.cash, D("-105.00"))

    def test_oversell_rejected_and_state_kept(self):
        L = m.Ledger(); L.buy("A", 5, 10, "d1")
        with self.assertRaises(ValueError):
            L.sell("A", 6, 12, "d2")
        with self.assertRaises(ValueError):
            L.sell("ZZZ", 1, 12, "d2")
        self.assertEqual(L.position("A"), D(5))
        self.assertEqual(L.cost_basis("A"), D("50.00"))
        self.assertEqual(L.total_realized(), D("0"))

    def test_fifo(self):
        L = m.Ledger(); L.buy("A", 10, "10", "d1"); L.buy("A", 10, "20", "d2")
        self.assertEqual(L.sell("A", 10, "30", "d3"), D("200.00"))
        self.assertEqual(L.cost_basis("A"), D("200.00"))

    def test_partial_lot(self):
        L = m.Ledger(); L.buy("A", 10, "10", "d1"); L.buy("A", 10, "20", "d2")
        self.assertEqual(L.sell("A", 15, "30", "d3"), D("250.00"))
        self.assertEqual(L.position("A"), D(5))
        self.assertEqual(L.cost_basis("A"), D("100.00"))
        self.assertEqual(L.sell("A", 5, "30", "d4"), D("50.00"))
        self.assertEqual(L.total_realized(), D("300.00"))
        self.assertEqual(L.symbols(), [])

    def test_split(self):
        L = m.Ledger(); L.buy("A", 4, "10.00", "d1"); L.split("A", 2)
        self.assertEqual(L.position("A"), D(8))
        self.assertEqual(L.average_cost("A"), D("5.00"))
        self.assertEqual(L.unrealized("A", "6"), D("8.00"))
        self.assertEqual(L.cost_basis("A"), D("40.00"))
        L.split("A", "0.5")
        self.assertEqual(L.position("A"), D(4))
        self.assertEqual(L.average_cost("A"), D("10.00"))

    def test_rounding_half_up(self):
        self.assertEqual(m.money("0.125"), D("0.13"))
        self.assertEqual(m.money("2.675"), D("2.68"))
        self.assertEqual(m.money("-0.125"), D("-0.13"))
        L = m.Ledger(); L.buy("A", 1, "1.000", "d"); g = L.sell("A", 1, "1.125", "e")
        self.assertEqual(g, D("0.13"))

    def test_fees_and_cash(self):
        L = m.Ledger(); L.buy("X", 3, "1.00", "d", fee="0.01")
        self.assertEqual(L.sell("X", 1, "1.10", "e"), D("0.10"))
        self.assertEqual(L.cost_basis("X"), D("2.01"))
        self.assertEqual(L.cash, D("-1.91"))
        L.sell("X", 2, "2.00", "f", fee="1.00")
        self.assertEqual(L.total_realized(), D("1.09"))

    def test_report(self):
        L = m.Ledger(); L.buy("B", "1.5", "100", "d"); L.buy("A", 2, "50", "d")
        self.assertEqual(L.report({"A": 55, "B": "90.5"}),
                         "A qty=2 avg=50.00 value=110.00 unrealized=10.00\n"
                         "B qty=1.5 avg=100.00 value=135.75 unrealized=-14.25")

    def test_validation(self):
        L = m.Ledger()
        for f in (lambda: L.buy("A", 0, 1, "d"), lambda: L.buy("A", -1, 1, "d"), lambda: L.split("A", 0)):
            with self.assertRaises(ValueError):
                f()


if __name__ == "__main__":
    unittest.main()
