"""The `money_compact` filter behind the dashboard/Reports stat tiles."""

from decimal import Decimal

from django.test import SimpleTestCase

from crm.templatetags.crm_extras import money_compact


class MoneyCompactTests(SimpleTestCase):
    def test_bands(self):
        cases = {
            None: "—",
            "": "—",
            0: "£0",
            999: "£999",
            Decimal("999.49"): "£999",
            1000: "£1K",
            1234: "£1.2K",
            12345: "£12.3K",
            450_000: "£450K",
            999_499: "£999K",
            999_500: "£1M",
            1_000_000: "£1M",
            2_000_000: "£2M",
            Decimal("30147691.00"): "£30.1M",
            105_400_000: "£105M",
            999_500_000: "£1B",
            1_250_000_000: "£1.3B",
            -1_500_000: "-£1.5M",
            "not a number": "not a number",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(money_compact(value), expected)

    def test_stat_tile_shows_compact_value_with_exact_tooltip(self):
        from django.template import Context, Template
        html = Template(
            '{% include "_stat_tile.html" with label="Funded" value=value kind="money" %}'
        ).render(Context({"value": Decimal("30147691")}))
        self.assertIn('title="£30,147,691"', html)
        self.assertIn("£30.1M", html)
