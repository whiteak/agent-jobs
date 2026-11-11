You maintain the module `billing.py` below. Users reported these problems:

1. Importing the module fails right away.
2. Totals are sometimes a cent off. For example an NL invoice with one item at 10.25 shows tax 2.15, but finance expects every amount rounded half-up to the cent: tax 2.15 is right there, yet for a subtotal of 0.50 the tax 0.105 must become 0.11, not 0.10. Line totals with discounts also leak more than two decimals into the subtotal; each line total must be rounded half-up to the cent before summing.
3. Monthly renewals scheduled from 31 January land on the wrong day or crash.
4. Page 1 of the invoice list skips the first invoices.
5. Searching for "acme" does not find "ACME Corp", and results come out oldest first.
6. Adding the same SKU twice with different discounts merges them, which loses the second discount. Only merge when SKU, unit price AND discount are all equal.

Fix all of these with the smallest reasonable changes. Keep every public name and signature the same and keep all behaviour that is not mentioned. Return the complete fixed `billing.py` in one ```python code block.

```python
