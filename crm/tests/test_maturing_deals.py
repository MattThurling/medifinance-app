"""`Deal.maturity_date` derivation and the term-end date on the deal form/detail."""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from .factories import make_associate, make_deal, make_quote


class MaturityDateTests(TestCase):
    def test_explicit_term_end_date_wins(self):
        deal = make_deal(
            term_end_date=date(2030, 1, 15),
            first_payment_date=date(2026, 1, 1),
        )
        deal.selected_quote = make_quote(deal, term=12)
        self.assertEqual(deal.maturity_date, date(2030, 1, 15))

    def test_derived_from_first_payment_and_quote_term(self):
        deal = make_deal(first_payment_date=date(2026, 1, 31))
        deal.selected_quote = make_quote(deal, term=13)
        deal.save()
        # 12 months after the first payment, clamped to month end.
        self.assertEqual(deal.maturity_date, date(2027, 1, 31))

    def test_commitment_end_wins_over_full_term(self):
        """A 25y/5y deal matures at the rate review — the re-brokering
        moment — not at year 25."""
        deal = make_deal(first_payment_date=date(2026, 1, 1))
        deal.selected_quote = make_quote(deal, term=300, commitment_months=60)
        deal.save()
        # 59 months after the first payment = the 60th (last committed) payment.
        self.assertEqual(deal.maturity_date, date(2030, 12, 1))

    def test_explicit_term_end_still_wins_over_commitment(self):
        deal = make_deal(
            term_end_date=date(2051, 1, 1),
            first_payment_date=date(2026, 1, 1),
        )
        deal.selected_quote = make_quote(deal, term=300, commitment_months=60)
        self.assertEqual(deal.maturity_date, date(2051, 1, 1))

    def test_none_without_either_source(self):
        deal = make_deal()
        self.assertIsNone(deal.maturity_date)
        deal.first_payment_date = date(2026, 1, 1)  # no quote → still None
        self.assertIsNone(deal.maturity_date)


class TermEndDateFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def test_round_trips_through_deal_form(self):
        deal = make_deal(owner=self.staff)
        response = self.client.post(
            reverse("crm:deal_update", args=[deal.pk]),
            {"name": deal.name, "owner": self.staff.pk, "term_end_date": "2031-06-28"},
        )
        deal.refresh_from_db()
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.term_end_date, date(2031, 6, 28))

    def test_detail_shows_term_end(self):
        deal = make_deal(owner=self.staff, term_end_date=date(2031, 6, 28))
        self.assertContains(self.client.get(deal.get_absolute_url()), "28 Jun 2031")
