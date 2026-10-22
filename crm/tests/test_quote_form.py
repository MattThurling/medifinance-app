"""Quote entry is type-split: Asset Finance gets the classic rate-band form,
every other deal type the Terms form (lender, status, reference + the terms
they came back with).

The AF tests double as a regression guard that the classic form is back to
its long-standing pre-commitment behaviour."""

from django.test import TestCase
from django.urls import reverse

from crm.forms import QuoteForm, TermsForm
from crm.models import Deal, Quote

from .factories import (
    make_associate,
    make_customer_with_deal,
    make_organisation,
    make_participation,
    make_rate_band,
)


def _deal(deal_type=None, amount=50_000, owner=None):
    _, deal = make_customer_with_deal(owner=owner)
    if deal_type:
        Deal.objects.filter(pk=deal.pk).update(type=deal_type)
        deal.refresh_from_db()
    if amount:
        make_participation(deal, amount=amount)
    return deal


class AssetFinanceQuoteFormTests(TestCase):
    """The classic form, restored: banded term select, rate scoped by term."""

    @classmethod
    def setUpTestData(cls):
        cls.deal = _deal()  # untyped = Asset Finance treatment
        cls.band60 = make_rate_band(term_months=60)

    def test_term_is_banded_select(self):
        form = QuoteForm(deal=self.deal)
        self.assertEqual([c for c, _ in form.fields["term"].choices if c != ""], [60])
        self.assertNotIn("commitment_months", form.fields)

    def test_saves_with_banded_term_and_rate(self):
        form = QuoteForm({"term": "60", "rate": str(self.band60.pk)}, deal=self.deal)
        self.assertTrue(form.is_valid(), form.errors)

    def test_unbanded_term_rejected(self):
        form = QuoteForm({"term": "300", "rate": str(self.band60.pk)}, deal=self.deal)
        self.assertFalse(form.is_valid())
        self.assertIn("term", form.errors)


class TermsFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.deal = _deal("commercial_finance", amount=None)
        cls.lender = make_organisation(name="Big Commercial Bank")

    def _data(self, **overrides):
        data = {"lender": str(self.lender.pk), "status": "approved",
                "interest_rate_percent": "9.50",
                "term": "300", "commitment_months": "60",
                "commission_percent": "2.00"}
        data.update(overrides)
        return data

    def test_saves_lender_term_commitment_no_rate(self):
        form = TermsForm(self._data(), deal=self.deal)
        self.assertTrue(form.is_valid(), form.errors)
        quote = form.save(commit=False)
        quote.deal = self.deal
        quote.save()
        self.assertIsNone(quote.rate)
        self.assertEqual(quote.lender, self.lender)
        self.assertEqual(quote.lender_org, self.lender)
        self.assertEqual((quote.term, quote.commitment_months, quote.priced_term), (300, 60, 60))
        # No funded amount on this deal yet, so payment is still TBC.
        self.assertIsNone(quote.monthly_payment)

    def test_commitment_optional(self):
        form = TermsForm(self._data(commitment_months=""), deal=self.deal)
        self.assertTrue(form.is_valid(), form.errors)
        quote = form.save(commit=False)
        quote.deal = self.deal
        quote.save()
        self.assertIsNone(quote.commitment_months)
        self.assertEqual(quote.priced_term, 300)  # full term

    def test_lender_required(self):
        form = TermsForm(self._data(lender=""), deal=self.deal)
        self.assertFalse(form.is_valid())
        self.assertIn("lender", form.errors)

    def test_commitment_not_shorter_than_term_rejected(self):
        form = TermsForm(
            self._data(term="60", commitment_months="60"), deal=self.deal
        )
        self.assertFalse(form.is_valid())
        self.assertIn("commitment_months", form.errors)

    def test_no_pricing_fields(self):
        form = TermsForm(deal=self.deal)
        for absent in ("rate", "deposit", "balloon", "repayment_profile"):
            self.assertNotIn(absent, form.fields)

    def test_rate_and_term_required_once_approved(self):
        form = TermsForm(self._data(interest_rate_percent="", term=""), deal=self.deal)
        self.assertFalse(form.is_valid())
        self.assertIn("interest_rate_percent", form.errors)
        self.assertIn("term", form.errors)

    def test_submitted_row_needs_only_the_lender(self):
        """A lender can be recorded the moment they're approached."""
        form = TermsForm({"lender": str(self.lender.pk), "status": "submitted"}, deal=self.deal)
        self.assertTrue(form.is_valid(), form.errors)
        quote = form.save(commit=False)
        quote.deal = self.deal
        quote.save()
        self.assertIsNone(quote.term)
        self.assertIsNone(quote.interest_rate_percent)
        self.assertIsNone(quote.monthly_payment)
        self.assertIn("term TBC", str(quote))

    def test_status_defaults_to_submitted(self):
        self.assertEqual(TermsForm(deal=self.deal).fields["status"].initial, "submitted")

    def test_commitment_without_term_rejected(self):
        form = TermsForm(
            {"lender": str(self.lender.pk), "status": "submitted", "commitment_months": "60"},
            deal=self.deal,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("commitment_months", form.errors)

    def test_asset_finance_form_untouched(self):
        form = QuoteForm(deal=_deal())
        self.assertNotIn("status", form.fields)
        self.assertNotIn("lender_reference", form.fields)
        self.assertTrue(form.fields["term"].required)

    def test_interest_rate_drives_mortgage_style_payment(self):
        """Standard ordinary-annuity PMT over the FULL term — the commercial
        lending convention, NOT the Asset Finance annuity-due formula.
        £100k at 9.5% p.a. over 300 months = £873.70/mo (hand-checkable),
        and commission is the broker's record, never loaded onto the payment."""
        from decimal import Decimal

        deal = _deal("commercial_finance", amount=100_000)
        form = TermsForm(self._data(), deal=deal)
        self.assertTrue(form.is_valid(), form.errors)
        quote = form.save(commit=False)
        quote.deal = deal
        quote.save()

        self.assertEqual(quote.monthly_payment, Decimal("873.70"))
        self.assertEqual(quote.interest_rate_percent, Decimal("9.50"))
        self.assertIsNone(quote.yield_percent)  # yields are a band concept
        self.assertIsNotNone(quote.apr)

        # Commission does not gross up the customer's payment.
        quote.commission_percent = None
        self.assertEqual(quote.monthly_payment, Decimal("873.70"))


class QuoteViewFormSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.af_deal = _deal(owner=cls.staff)
        cls.cf_deal = _deal("commercial_finance", amount=None, owner=cls.staff)
        cls.lender = make_organisation(name="CF Lender Ltd")
        make_rate_band(term_months=60)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_create_view_picks_form_by_type(self):
        response = self.client.get(reverse("crm:quote_create") + f"?deal={self.af_deal.pk}")
        self.assertIsInstance(response.context["form"], QuoteForm)
        response = self.client.get(reverse("crm:quote_create") + f"?deal={self.cf_deal.pk}")
        self.assertIsInstance(response.context["form"], TermsForm)
        self.assertContains(response, "Search organisations")  # lender combobox

    def test_cf_create_bypasses_funded_amount_guard(self):
        assert self.cf_deal.funded_amount is None
        response = self.client.post(
            reverse("crm:quote_create") + f"?deal={self.cf_deal.pk}",
            {"lender": str(self.lender.pk), "status": "approved",
             "interest_rate_percent": "9.50",
             "term": "300", "commitment_months": "60"},
        )
        self.assertEqual(response.status_code, 302)
        quote = self.cf_deal.quotes.get()
        self.assertEqual(quote.lender, self.lender)
        self.assertEqual(quote.status, "approved")

    def test_update_view_picks_form_by_type(self):
        from .factories import make_commercial_quote, make_quote

        af_quote = make_quote(self.af_deal)
        cf_quote = make_commercial_quote(self.cf_deal, lender=self.lender)
        response = self.client.get(reverse("crm:quote_update", args=[af_quote.pk]))
        self.assertIsInstance(response.context["form"], QuoteForm)
        response = self.client.get(reverse("crm:quote_update", args=[cf_quote.pk]))
        self.assertIsInstance(response.context["form"], TermsForm)


class RateOptionsEndpointTests(TestCase):
    """Back to plain term keying — the commercial form never calls it."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = _deal(owner=cls.staff)
        cls.band60 = make_rate_band(term_months=60)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_bands_matched_on_term(self):
        url = reverse("crm:quote_rate_options")
        response = self.client.get(url, {"term": "60", "deal": self.deal.pk})
        self.assertContains(response, str(self.band60.pk))
        response = self.client.get(url, {"term": "300", "deal": self.deal.pk})
        self.assertNotContains(response, str(self.band60.pk))
