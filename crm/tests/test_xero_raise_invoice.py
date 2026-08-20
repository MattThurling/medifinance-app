"""Raise-invoice prefill: the commission invoice is addressed to the funder —
the selected proposal's lender — never to the client organisation."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .factories import (
    make_associate,
    make_deal,
    make_organisation,
    make_proposal,
    make_xero_connection,
)


class RaiseInvoicePrefillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.finance = make_associate(is_finance=True)

    def setUp(self):
        self.client.force_login(self.finance)
        make_xero_connection()

    def _deal_with_lender(self, **deal_extra):
        deal = make_deal(**deal_extra)
        lender = make_organisation(name="Big Lender Ltd")
        proposal = make_proposal(deal, lender=lender, proposal_number="BL-9876")
        deal.selected_proposal = proposal
        deal.save(update_fields=["selected_proposal"])
        return deal

    def test_prefills_lender_not_client(self):
        deal = self._deal_with_lender(commission=Decimal("1500.00"))
        response = self.client.get(reverse("crm:deal_raise_invoice", args=[deal.pk]))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["contact_name"], "Big Lender Ltd")
        self.assertNotEqual(form.initial["contact_name"], deal.organisation.name)
        self.assertEqual(form.initial["amount"], Decimal("1500.00"))
        # The lender's proposal reference rides along for reconciliation.
        self.assertEqual(form.initial["reference"], f"{deal.name} · BL-9876")

    def test_reference_without_proposal_number(self):
        deal = make_deal()
        proposal = make_proposal(deal)
        deal.selected_proposal = proposal
        deal.save(update_fields=["selected_proposal"])
        response = self.client.get(reverse("crm:deal_raise_invoice", args=[deal.pk]))
        self.assertEqual(response.context["form"].initial["reference"], deal.name)

    def test_requires_selected_proposal(self):
        deal = make_deal()
        response = self.client.get(
            reverse("crm:deal_raise_invoice", args=[deal.pk]), follow=True,
        )
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertContains(response, "Select a Proposal on this deal first")
