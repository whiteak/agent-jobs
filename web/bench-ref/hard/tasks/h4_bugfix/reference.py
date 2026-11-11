"""Portfolio ledger: FIFO lots, realized gains, splits, fees. Money is Decimal, rounded to cents (half up)."""
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

CENT = Decimal("0.01")


def money(x):
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


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
        total = qty * price + fee
        self.lots[symbol].append(Lot(qty, total / qty, date))
        self.cash -= money(total)
        self.history.append(("buy", symbol, qty, date))

    def sell(self, symbol, qty, price, date, fee=0):
        qty, price, fee = Decimal(str(qty)), Decimal(str(price)), Decimal(str(fee))
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > self.position(symbol):
            raise ValueError("not enough shares")
        remaining = qty
        cost = Decimal("0")
        lots = self.lots[symbol]
        while remaining > 0:
            lot = lots[0]
            take = min(lot.qty, remaining)
            cost += take * lot.unit_cost
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                lots.pop(0)
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
            lot.unit_cost = lot.unit_cost / ratio

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
