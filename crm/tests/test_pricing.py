"""Golden-value tests for the pure pricing functions in `crm/pricing.py`.

The math is load-bearing — it has to match the broker's spreadsheet, and every
quote in the system (internal AND external API) flows through it. Pinning the
outputs for one known case stops the refactor (or any future tweak) from
silently drifting."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from crm import pricing

from .factories import make_organisation, make_rate_band


class PricingFunctionsTests(TestCase):
    """One canonical case (£25,000 / 60 months / 10% yield) pinned to the values
    the broker spreadsheet produces. If any of these change, the broker quote
    has drifted."""

    PRINCIPAL = Decimal("25000")
    TERM = 60

    @classmethod
    def setUpTestData(cls):
        cls.band = make_rate_band(
            term_months=cls.TERM,
            min_amount=1_000,
            max_amount=1_000_000,
            yield_percent="10.00",
        )

    def test_monthly_payment_no_commission(self):
        result = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        self.assertEqual(result, Decimal("526.79"))

    def test_monthly_payment_with_commission_is_higher(self):
        base = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        with_comm = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
            commission_percent=Decimal("1.5"),
        )
        self.assertEqual(with_comm, Decimal("534.69"))
        self.assertGreater(with_comm, base)

    def test_apr_no_commission(self):
        monthly = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        result = pricing.apr(
            principal=self.PRINCIPAL, monthly_payment=monthly, term_months=self.TERM,
        )
        self.assertEqual(result, Decimal("9.64"))

    def test_apr_rises_with_commission(self):
        base_m = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        comm_m = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
            commission_percent=Decimal("1.5"),
        )
        base_apr = pricing.apr(
            principal=self.PRINCIPAL, monthly_payment=base_m, term_months=self.TERM,
        )
        comm_apr = pricing.apr(
            principal=self.PRINCIPAL, monthly_payment=comm_m, term_months=self.TERM,
        )
        self.assertEqual(comm_apr, Decimal("10.29"))
        self.assertGreater(comm_apr, base_apr)

    def test_flat_rate(self):
        monthly = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        result = pricing.flat_rate(
            principal=self.PRINCIPAL, monthly_payment=monthly, term_months=self.TERM,
        )
        self.assertEqual(result, Decimal("5.29"))

    def test_total_interest(self):
        monthly = pricing.monthly_payment(
            principal=self.PRINCIPAL, rate_band=self.band, term_months=self.TERM,
        )
        result = pricing.total_interest(
            principal=self.PRINCIPAL, monthly_payment=monthly, term_months=self.TERM,
        )
        self.assertEqual(result, Decimal("6607.40"))

    def test_total_interest_matches_monthly_times_term_minus_principal(self):
        monthly = Decimal("500.00")
        result = pricing.total_interest(
            principal=Decimal("10000"), monthly_payment=monthly, term_months=24,
        )
        # 500 * 24 - 10000 = 12000 - 10000 = 2000
        self.assertEqual(result, Decimal("2000.00"))


class PricingEdgeCaseTests(TestCase):
    def test_missing_inputs_return_none(self):
        band = make_rate_band()
        self.assertIsNone(pricing.monthly_payment(
            principal=None, rate_band=band, term_months=60,
        ))
        self.assertIsNone(pricing.monthly_payment(
            principal=Decimal("1000"), rate_band=None, term_months=60,
        ))
        self.assertIsNone(pricing.monthly_payment(
            principal=Decimal("1000"), rate_band=band, term_months=0,
        ))

    def test_apr_returns_none_for_missing_inputs(self):
        self.assertIsNone(pricing.apr(
            principal=None, monthly_payment=Decimal("100"), term_months=12,
        ))
        self.assertIsNone(pricing.apr(
            principal=Decimal("1000"), monthly_payment=None, term_months=12,
        ))


class RepaymentScheduleTests(TestCase):
    """Amortisation of the canonical case (£25,000 / 60 months → £526.79/mo),
    anchored to a month-end first payment so the date clamping is exercised."""

    PRINCIPAL = Decimal("25000")
    MONTHLY = Decimal("526.79")
    TERM = 60
    FIRST = date(2026, 1, 31)

    def _schedule(self):
        return pricing.repayment_schedule(
            principal=self.PRINCIPAL,
            monthly_payment=self.MONTHLY,
            term_months=self.TERM,
            first_payment_date=self.FIRST,
        )

    def test_one_row_per_month(self):
        rows = self._schedule()
        self.assertEqual(len(rows), self.TERM)
        self.assertEqual([r["number"] for r in rows], list(range(1, self.TERM + 1)))

    def test_due_dates_step_monthly_clamping_to_month_end(self):
        rows = self._schedule()
        self.assertEqual(rows[0]["due_date"], date(2026, 1, 31))
        self.assertEqual(rows[1]["due_date"], date(2026, 2, 28))  # clamped
        self.assertEqual(rows[2]["due_date"], date(2026, 3, 31))  # back to the 31st
        self.assertEqual(rows[-1]["due_date"], date(2030, 12, 31))

    def test_first_row_split_matches_implied_rate(self):
        row = self._schedule()[0]
        self.assertEqual(row["payment"], Decimal("526.79"))
        self.assertEqual(row["interest"], Decimal("200.89"))
        self.assertEqual(row["principal"], Decimal("325.90"))
        self.assertEqual(row["balance"], Decimal("24674.10"))

    def test_principal_repaid_in_full_and_balance_reaches_zero(self):
        rows = self._schedule()
        self.assertEqual(sum(r["principal"] for r in rows), self.PRINCIPAL)
        self.assertEqual(rows[-1]["balance"], Decimal("0.00"))

    def test_final_payment_absorbs_rounding(self):
        rows = self._schedule()
        # Every payment is the quoted monthly except the last, which settles
        # the balance exactly (here a penny over).
        self.assertTrue(all(r["payment"] == self.MONTHLY for r in rows[:-1]))
        self.assertEqual(rows[-1]["payment"], Decimal("526.80"))

    def test_zero_interest_schedule_is_all_principal(self):
        rows = pricing.repayment_schedule(
            principal=Decimal("1200"),
            monthly_payment=Decimal("100"),
            term_months=12,
            first_payment_date=date(2026, 7, 1),
        )
        self.assertTrue(all(r["interest"] == 0 for r in rows))
        self.assertTrue(all(r["payment"] == Decimal("100") for r in rows))
        self.assertEqual(rows[-1]["balance"], Decimal("0.00"))

    def test_missing_inputs_return_none(self):
        self.assertIsNone(pricing.repayment_schedule(
            principal=None, monthly_payment=self.MONTHLY,
            term_months=self.TERM, first_payment_date=self.FIRST,
        ))
        self.assertIsNone(pricing.repayment_schedule(
            principal=self.PRINCIPAL, monthly_payment=None,
            term_months=self.TERM, first_payment_date=self.FIRST,
        ))
        self.assertIsNone(pricing.repayment_schedule(
            principal=self.PRINCIPAL, monthly_payment=self.MONTHLY,
            term_months=self.TERM, first_payment_date=None,
        ))


class CheapestRateBandTests(TestCase):
    def test_returns_lowest_yield_band_for_term_and_amount(self):
        lender_a = make_organisation(name="Lender A")
        lender_b = make_organisation(name="Lender B")
        lender_c = make_organisation(name="Lender C")
        # All three cover £25k @ 60m — A is the cheapest by yield.
        make_rate_band(organisation=lender_a, term_months=60,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="8.50")
        make_rate_band(organisation=lender_b, term_months=60,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="9.75")
        make_rate_band(organisation=lender_c, term_months=60,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="11.00")

        band = pricing.cheapest_rate_band(term_months=60, amount=25_000)
        self.assertIsNotNone(band)
        self.assertEqual(band.organisation, lender_a)

    def test_returns_none_when_amount_outside_every_band(self):
        make_rate_band(min_amount=1_000, max_amount=10_000, yield_percent="9")
        self.assertIsNone(pricing.cheapest_rate_band(term_months=60, amount=999_999))

    def test_returns_none_when_no_band_for_term(self):
        make_rate_band(term_months=60, yield_percent="9")
        self.assertIsNone(pricing.cheapest_rate_band(term_months=12, amount=25_000))

    def test_inactive_bands_are_skipped(self):
        cheap = make_rate_band(term_months=60, min_amount=1_000, max_amount=1_000_000,
                               yield_percent="5.00")
        cheap.is_active = False
        cheap.save(update_fields=["is_active"])
        expensive = make_rate_band(term_months=60, min_amount=1_000, max_amount=1_000_000,
                                   yield_percent="12.00")
        band = pricing.cheapest_rate_band(term_months=60, amount=25_000)
        self.assertEqual(band, expensive)
