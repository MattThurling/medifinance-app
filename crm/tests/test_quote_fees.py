"""Commission and fees live on the quote (a Terms row on commercial deals),
not the deal. The deal shows the selected quote's commission in £ — its
commission % of the amount financed — and its document fee split.

Document fee, document fee split and option to purchase fee are Asset Finance
only; Source and its free-text Identifier are on the deal form."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from crm import stats
from crm.forms import DealForm, QuoteForm, TermsForm
from crm.models import Deal, Quote

from .factories import (
    make_associate,
    make_commercial_quote,
    make_deal,
    make_participation,
    make_rate_band,
    select_quote,
)

FEE_FIELDS = ("document_fee", "document_fee_split", "option_to_purchase_fee")


def _sql_commission(deal):
    return Deal.objects.annotate(c=stats.commission_amount_expr()).get(pk=deal.pk).c


class CommissionAmountTests(TestCase):
    def test_percent_of_finance_amount_net_of_deposit_and_balloon(self):
        deal = make_deal()
        make_participation(deal, amount="50000.00")
        quote = select_quote(
            deal, commission_percent="2.5",
            deposit=Decimal("5000"), balloon=Decimal("1000"),
        )
        # 2.5% of (50,000 − 5,000 − 1,000)
        self.assertEqual(quote.commission_amount, Decimal("1100.00"))
        self.assertEqual(deal.commission_amount, Decimal("1100.00"))
        self.assertEqual(_sql_commission(deal), Decimal("1100.00"))

    def test_rounds_to_the_penny(self):
        deal = make_deal()
        make_participation(deal, amount="1234.56")
        select_quote(deal, commission_percent="3.33")
        self.assertEqual(deal.commission_amount, Decimal("41.11"))  # 41.110848
        self.assertEqual(_sql_commission(deal), Decimal("41.11"))

    def test_none_without_a_selected_quote_percent_or_funding(self):
        no_quote = make_deal()
        make_participation(no_quote)
        unselected = make_deal()
        make_participation(unselected)
        make_commercial_quote(unselected, commission_percent=Decimal("3"))
        no_percent = make_deal()
        make_participation(no_percent)
        select_quote(no_percent)
        unfunded = make_deal()
        select_quote(unfunded, commission_percent="3")
        for deal in (no_quote, unselected, no_percent, unfunded):
            self.assertIsNone(deal.commission_amount)
            self.assertIsNone(_sql_commission(deal))

    def test_deal_no_longer_has_its_own_commission_or_document_fee(self):
        names = {f.name for f in Deal._meta.get_fields()}
        self.assertNotIn("commission", names)
        self.assertNotIn("document_fee", names)
        self.assertNotIn("commission", DealForm.base_fields)
        self.assertNotIn("document_fee", DealForm.base_fields)


class FeeFieldsAreAssetFinanceOnlyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)  # untyped = Asset Finance treatment
        make_participation(cls.deal, amount="50000.00")
        cls.band = make_rate_band(term_months=60)

    def test_quote_form_has_fees_and_terms_form_does_not(self):
        for name in FEE_FIELDS:
            self.assertIn(name, QuoteForm.base_fields)
            self.assertNotIn(name, TermsForm.base_fields)

    def test_quote_form_saves_fees(self):
        form = QuoteForm(
            {
                "term": "60", "rate": str(self.band.pk), "commission_percent": "3",
                "document_fee": "250", "document_fee_split": "125.50",
                "option_to_purchase_fee": "100",
            },
            deal=self.deal,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.instance.deal = self.deal
        quote = form.save()
        self.assertEqual(quote.document_fee, Decimal("250"))
        self.assertEqual(quote.document_fee_split, Decimal("125.50"))
        self.assertEqual(quote.option_to_purchase_fee, Decimal("100"))

    def test_fee_inputs_only_on_the_asset_finance_quote_page(self):
        self.client.force_login(self.staff)
        url = reverse("crm:quote_create")
        response = self.client.get(url, {"deal": self.deal.pk})
        for name in FEE_FIELDS:
            self.assertContains(response, f'name="{name}"')

        commercial = make_deal(owner=self.staff, type=Deal.Type.COMMERCIAL_FINANCE)
        response = self.client.get(url, {"deal": commercial.pk})
        self.assertContains(response, 'name="commission_percent"')
        for name in FEE_FIELDS:
            self.assertNotContains(response, f'name="{name}"')


class DealPagesShowCommissionAndSplitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def _deal(self, **quote_extra):
        deal = make_deal(owner=self.staff, **quote_extra.pop("deal", {}))
        make_participation(deal, amount="40000.00")
        select_quote(deal, commission_percent="2", **quote_extra)
        return deal

    def test_asset_finance_deal_shows_amount_percent_and_split(self):
        deal = self._deal(document_fee_split=Decimal("75.00"))
        for name in ("deal_detail", "deal_overview"):
            response = self.client.get(reverse(f"crm:{name}", args=[deal.pk]))
            self.assertContains(response, "£800.00")
            self.assertContains(response, "2.00%")
            self.assertContains(response, "£75.00")
            self.assertContains(response, "ocument fee split")

    def test_commercial_deal_shows_commission_amount(self):
        deal = self._deal(deal={"type": Deal.Type.COMMERCIAL_FINANCE})
        for name in ("deal_detail", "deal_overview"):
            response = self.client.get(reverse(f"crm:{name}", args=[deal.pk]))
            self.assertContains(response, "£800.00")
            self.assertContains(response, "Commission %")

    def test_no_split_row_when_the_quote_has_none(self):
        deal = self._deal()
        response = self.client.get(reverse("crm:deal_overview", args=[deal.pk]))
        self.assertNotContains(response, "Document fee split</dt>  <dd>£")


class SourceIdentifierTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_form_renders_source_and_identifier(self):
        for url in (reverse("crm:deal_create"), reverse("crm:deal_update", args=[self.deal.pk])):
            response = self.client.get(url)
            self.assertContains(response, 'name="source"')
            self.assertContains(response, 'name="source_identifier"')

    def test_form_saves_and_pages_show_identifier(self):
        data = {
            "name": self.deal.name,
            "customer": self.deal.customer_id or "",
            "organisation": self.deal.organisation_id or "",
            "owner": self.staff.pk,
            "source": Deal.Source.values[0],
            "source_identifier": "Dentistry Show 2026",
        }
        form = DealForm(data, instance=self.deal)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.source_identifier, "Dentistry Show 2026")
        for name in ("deal_detail", "deal_overview"):
            response = self.client.get(reverse(f"crm:{name}", args=[self.deal.pk]))
            self.assertContains(response, "Dentistry Show 2026")

    def test_identifier_is_optional(self):
        self.assertFalse(DealForm.base_fields["source_identifier"].required)
