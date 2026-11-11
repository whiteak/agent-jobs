"""Billing helpers for a small subscription service."""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import calendar

TAX_RATES = {"NL": Decimal("0.21"), "DE": Decimal("0.19"), "AU": Decimal("0.10"), "US": Decimal("0")}


@dataclass
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int = 1
    discount_pct: Decimal = Decimal("0")

    def total(self) -> Decimal:
        gross = self.unit_price * self.quantity
        return money(gross - gross * self.discount_pct / 100)


@dataclass
class Invoice:
    customer: str
    country: str
    items: list = field(default_factory=list)
    issued: date = field(default_factory=date.today)
    notes: list = field(default_factory=list)

    def add(self, item: LineItem) -> None:
        for existing in self.items:
            if (existing.sku == item.sku and existing.unit_price == item.unit_price
                    and existing.discount_pct == item.discount_pct):
                existing.quantity += item.quantity
                return
        self.items.append(item)

    def subtotal(self) -> Decimal:
        return money(sum((i.total() for i in self.items), Decimal("0")))

    def tax(self) -> Decimal:
        rate = TAX_RATES.get(self.country, Decimal("0"))
        return money(self.subtotal() * rate)

    def total(self) -> Decimal:
        return money(self.subtotal() + self.tax())

    def due_date(self, terms_days: int = 30) -> date:
        return self.issued + timedelta(days=terms_days)


def money(x) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the last day of a shorter month."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def prorate(price: Decimal, start: date, end: date, period_start: date, period_end: date) -> Decimal:
    """Charge for the part of [period_start, period_end) covered by [start, end), by days."""
    s = max(start, period_start)
    e = min(end, period_end)
    if e <= s:
        return Decimal("0.00")
    covered = (e - s).days
    total = (period_end - period_start).days
    return money(price * covered / total)


def paginate(invoices: list, page: int, per_page: int = 20) -> list:
    """Page numbers start at 1."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    start = (page - 1) * per_page
    return invoices[start:start + per_page]


def find_customer(invoices: list, query: str) -> list:
    """Invoices whose customer name contains `query`, ignoring case, newest first."""
    q = query.lower()
    hits = [inv for inv in invoices if q in inv.customer.lower()]
    return sorted(hits, key=lambda inv: inv.issued, reverse=True)
