"""Client price comparison: grouping by EAN, then by brand + title."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "niche"))
import client_report as cr  # noqa: E402


def row(store, title, price, gtin="", vendor="Lindt", variant=""):
    return {"store": store, "product_title": title, "variant_title": variant, "vendor": vendor,
            "price": str(price), "gtin": gtin}


class ClientReport(unittest.TestCase):
    def test_groups_by_ean_and_title(self):
        rows = [
            row("A", "Excellence 70%", 12.0, "4000539003502"),
            row("B", "Lindt Excellence 70 % 100g", 10.0, "04000539003502"),   # same EAN, other name
            row("A", "Gorzka 90%", 9.0),
            row("B", "GORZKA 90 %", 9.9),                                      # same brand + title
            row("C", "Only here", 5.0, vendor="X"),
        ]
        header, table, st = cr.build(rows)
        self.assertEqual(header[8:], ["A", "B", "C"])
        self.assertEqual(st["compared"], 2)
        self.assertEqual(st["by_ean"], 1)
        first = table[0]                      # biggest difference first: 12 vs 10 = +20%
        self.assertEqual(first[2], "4000539003502")
        self.assertEqual((first[4], first[5], first[6], first[7]), (10.0, 12.0, 20.0, "B"))
        self.assertEqual(table[-1][3], 1)     # single-shop products at the end

    def test_cheapest_variant_per_shop(self):
        rows = [row("A", "T", 5.0, "12345670"), row("A", "T", 4.0, "12345670"), row("B", "T", 6.0, "12345670")]
        _, table, _ = cr.build(rows)
        self.assertEqual(table[0][4:6], [4.0, 6.0])


if __name__ == "__main__":
    unittest.main()
