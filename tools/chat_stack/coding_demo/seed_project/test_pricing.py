# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from decimal import Decimal

from pricing import LineItem, apply_discount, line_total, order_total


def items(*prices_and_quantities):
    return [LineItem(f"SKU{i}", Decimal(price), quantity) for i, (price, quantity) in enumerate(prices_and_quantities)]


def test_line_total_multiplies_price_by_quantity():
    assert line_total(LineItem("A", Decimal("2.50"), 4)) == Decimal("10.00")


def test_discount_takes_a_whole_number_percentage():
    assert apply_discount(Decimal("100"), 15) == Decimal("85")


def test_small_order_pays_shipping_and_tax_on_goods_only():
    assert order_total(items(("10.00", 2))) == Decimal("26.59")


def test_discount_that_drops_below_threshold_brings_back_shipping():
    assert order_total(items(("55.00", 1)), discount_percent=10) == Decimal("58.45")


def test_discounted_order_above_threshold_ships_free():
    assert order_total(items(("30.00", 2)), discount_percent=10) == Decimal("58.32")
