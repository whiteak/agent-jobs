Our portfolio ledger module (below) has several bugs. Users reported:

1. "I bought 10 shares at $10 with a $5 commission and the app says my cost basis is $95. Surely it's $105?"
2. "I sold shares I never owned and it just let me. My position went negative."
3. "Selling uses the wrong lots. We're supposed to be FIFO for tax purposes, and the realized gain is off when I have lots at different prices."
4. "When I sell only part of a lot, the realized gain is way too low."
5. "After a 2-for-1 split, my unrealized gain doubled overnight even though the stock price halved."
6. "Some amounts are off by a cent: a gain of 0.125 shows as 0.12, but our policy is round half up to cents (0.13)."

Fix ALL the bugs. Keep the public API exactly the same (class and method names, arguments, return types, the report format). Money stays Decimal; cost per share is kept unrounded, and only the amounts that are already rounded with `money()` are rounded. There may be bugs nobody reported yet; fix any you find, but do not change behaviour that is correct.

Return the complete fixed module in one ```python code block.

```python
"""Portfolio ledger: FIFO lots, realized gains, splits, fees. Money is Decimal, rounded to cents (half up)."""
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

CENT = Decimal("0.01")


def money(x):
    return Decimal(str(round(float(x), 2))).quantize(CENT)


class Lot:
    def __init__(self, qty, unit_cost, date):
        self.qty = Decimal(qty)
        self.unit_cost = Decimal(unit_cost)   # unrounded cost per share, fees included
        self.date = date


class Ledger:
    def __init__(self):
        self.lots = defaultdict(list)          # symbol -> [Lot], oldest first
        self.realized = defaultdict(Decimal)   # symbol -> realized gain (Decimal, rounded per sale)
        self.cash = Decimal("0")
        self.history = []

    def buy(self, symbol, qty, price, date, fee=0):
        qty, price, fee = Decimal(str(qty)), Decimal(str(price)), Decimal(str(fee))
        if qty <= 0:
            raise ValueError("qty must be positive")
        total = qty * price - fee
        self.lots[symbol].append(Lot(qty, total / qty, date))
        self.cash -= money(total)
        self.history.append(("buy", symbol, qty, date))

    def sell(self, symbol, qty, price, date, fee=0):
        qty, price, fee = Decimal(str(qty)), Decimal(str(price)), Decimal(str(fee))
        if qty <= 0:
            raise ValueError("qty must be positive")
        remaining = qty
        cost = Decimal("0")
        lots = self.lots[symbol]
        while remaining > 0:
            lot = lots[-1]
            take = min(lot.qty, remaining)
            cost += lot.qty * lot.unit_cost
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                lots.pop()
        proceeds = qty * price - fee
        gain = money(proceeds - cost)
        self.realized[symbol] += gain
        self.cash += money(proceeds)
        self.history.append(("sell", symbol, qty, date))
        return gain

    def split(self, symbol, ratio):
        """ratio 2 means each share becomes 2 shares (2-for-1). Fractional ratios allowed (e.g. 0.5)."""
        ratio = Decimal(str(ratio))
        if ratio <= 0:
            raise ValueError("bad ratio")
        for lot in self.lots[symbol]:
            lot.qty = lot.qty * ratio

    def position(self, symbol):
        return sum((lot.qty for lot in self.lots[symbol]), Decimal("0"))

    def cost_basis(self, symbol):
        return money(sum((lot.qty * lot.unit_cost for lot in self.lots[symbol]), Decimal("0")))

    def average_cost(self, symbol):
        pos = self.position(symbol)
        if pos == 0:
            return Decimal("0.00")
        return money(sum((lot.qty * lot.unit_cost for lot in self.lots[symbol]), Decimal("0")) / pos)

    def unrealized(self, symbol, price):
        price = Decimal(str(price))
        return money(self.position(symbol) * price) - self.cost_basis(symbol)

    def total_realized(self):
        return sum(self.realized.values(), Decimal("0"))

    def symbols(self):
        return sorted(s for s in self.lots if self.position(s) > 0)

    def report(self, prices):
        """One line per held symbol, sorted: 'SYM qty=Q avg=A value=V unrealized=U'."""
        lines = []
        for s in self.symbols():
            q = self.position(s)
            qs = format(q.normalize(), "f")
            v = money(q * Decimal(str(prices[s])))
            lines.append(f"{s} qty={qs} avg={self.average_cost(s)} value={v} unrealized={self.unrealized(s, prices[s])}")
        return "\n".join(lines)
```
