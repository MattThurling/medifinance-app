"""Commercial Finance deals present participations as plain "Funded amounts"
— just an amount, no supplier, no invoice — while Asset Finance (and untyped)
deals keep the full supplier/invoice participation UI."""

from django.test import TestCase
from django.urls import reverse

from crm.forms import ParticipationForm
from crm.models import Deal

from .factories import make_associate, make_deal, make_organisation, make_participation


class FundedAmountTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.commercial = make_deal(owner=cls.staff, type=Deal.Type.COMMERCIAL_FINANCE)
        cls.asset = make_deal(owner=cls.staff, type=Deal.Type.ASSET_FINANCE)

    def setUp(self):
        self.client.force_login(self.staff)


class DealDetailDisplayTests(FundedAmountTestCase):
    def test_commercial_deal_shows_funded_amounts_not_suppliers(self):
        p = make_participation(self.commercial, amount="25000.00")
        response = self.client.get(self.commercial.get_absolute_url())
        self.assertContains(response, "Funded amounts")
        self.assertContains(response, "circle-pound-sterling")
        self.assertNotContains(response, "Suppliers")
        # No invoice column → no per-participation "Request" button.
        self.assertNotContains(
            response, reverse("crm:participation_request_invoice", args=[p.pk])
        )

    def test_asset_deal_keeps_supplier_section(self):
        p = make_participation(self.asset)
        response = self.client.get(self.asset.get_absolute_url())
        self.assertContains(response, "Suppliers")
        self.assertNotContains(response, "Funded amounts")
        self.assertContains(
            response, reverse("crm:participation_request_invoice", args=[p.pk])
        )

    def test_untyped_deal_keeps_supplier_section(self):
        deal = make_deal(owner=self.staff)
        self.assertContains(self.client.get(deal.get_absolute_url()), "Suppliers")

    def test_amount_card_shows_type_label_with_asset_fallback(self):
        self.assertContains(
            self.client.get(self.commercial.get_absolute_url()), "Commercial Finance"
        )
        untyped = make_deal(owner=self.staff)
        self.assertContains(self.client.get(untyped.get_absolute_url()), "Asset Finance")


class ParticipationFormFieldTests(FundedAmountTestCase):
    def test_supplier_and_invoice_fields_dropped_for_commercial_deal(self):
        form = ParticipationForm(deal=self.commercial)
        self.assertEqual(set(form.fields), {"amount", "description"})

    def test_all_fields_kept_for_asset_deal_and_without_deal(self):
        expected = {
            "amount", "organisation", "description",
            "invoice_number", "invoice_contact", "invoice",
        }
        self.assertEqual(set(ParticipationForm(deal=self.asset).fields), expected)
        self.assertEqual(set(ParticipationForm().fields), expected)


class ParticipationFormViewTests(FundedAmountTestCase):
    def test_create_page_for_commercial_deal_uses_funded_amount_wording(self):
        response = self.client.get(
            reverse("crm:participation_create"), {"deal": self.commercial.pk}
        )
        self.assertContains(response, "New funded amount")
        self.assertNotContains(response, 'name="invoice_number"')
        self.assertNotContains(response, "Search organisations…")

    def test_create_page_for_asset_deal_keeps_supplier_wording(self):
        response = self.client.get(
            reverse("crm:participation_create"), {"deal": self.asset.pk}
        )
        self.assertContains(response, "New supplier")
        self.assertContains(response, 'name="invoice_number"')

    def test_create_posts_amount_only(self):
        response = self.client.post(
            reverse("crm:participation_create") + f"?deal={self.commercial.pk}",
            {"amount": "15000.00", "description": "Working capital"},
        )
        participation = self.commercial.participations.get()
        self.assertRedirects(response, self.commercial.get_absolute_url())
        self.assertEqual(participation.amount, 15000)
        self.assertEqual(participation.description, "Working capital")

    def test_posted_supplier_fields_ignored_for_commercial_deal(self):
        org = make_organisation()
        self.client.post(
            reverse("crm:participation_create") + f"?deal={self.commercial.pk}",
            {"amount": "1000.00", "organisation": org.pk, "invoice_number": "INV-1"},
        )
        participation = self.commercial.participations.get()
        self.assertIsNone(participation.organisation)
        self.assertEqual(participation.invoice_number, "")

    def test_update_page_for_commercial_deal(self):
        participation = make_participation(self.commercial)
        response = self.client.get(
            reverse("crm:participation_update", args=[participation.pk])
        )
        self.assertContains(response, "Edit funded amount")
        self.assertNotContains(response, 'name="invoice_number"')

    def test_update_page_for_asset_deal(self):
        participation = make_participation(self.asset)
        response = self.client.get(
            reverse("crm:participation_update", args=[participation.pk])
        )
        self.assertContains(response, "Edit supplier")
        self.assertContains(response, 'name="invoice_number"')
