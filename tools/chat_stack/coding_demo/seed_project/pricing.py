# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Order pricing for the shop checkout."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

FREE_SHIPPING_THRESHOLD = Decimal("50.00")
SHIPPING_FEE = Decimal("4.99")
CENT = Decimal("0.01")


@dataclass
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int


def line_total(item: LineItem) -> Decimal:
    return item.unit_price * item.quantity


def order_subtotal(items: list[LineItem]) -> Decimal:
    return sum((line_total(item) for item in items), Decimal("0"))


def apply_discount(amount: Decimal, percent: int) -> Decimal:
    """Reduce amount by a percentage given as a whole number, e.g. 15 for 15%."""
    return amount - amount * percent


def order_total(items: list[LineItem], discount_percent: int = 0, tax_rate: Decimal = Decimal("0.08")) -> Decimal:
    """Total to charge, rounded to cents.

    The discount applies to the goods. Shipping is free when the discounted subtotal reaches
    FREE_SHIPPING_THRESHOLD. Tax applies to the discounted goods, not to shipping.
    """
    subtotal = order_subtotal(items)
    discounted = apply_discount(subtotal, discount_percent)
    shipping = Decimal("0") if subtotal >= FREE_SHIPPING_THRESHOLD else SHIPPING_FEE
    total = discounted * (1 + tax_rate) + shipping
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
