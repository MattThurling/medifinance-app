"""The Repayments schedule section — rendered on the staff deal page and the
customer portal once a first payment date and a priced selected quote exist."""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from .factories import (
    make_admin,
    make_customer_with_deal,
    make_participation,
    make_quote,
)


def _fund_and_select_quote(deal):
    """£25,000 funded + the canonical 60m/10% quote selected, first payment
    1 Aug 2026 — the same golden case test_pricing pins (£526.79/mo)."""
    make_participation(deal, amount="25000.00")
    quote = make_quote(deal)
    deal.selected_quote = quote
    deal.first_payment_date = date(2026, 8, 1)
    deal.save(update_fields=["selected_quote", "first_payment_date"])
    return quote


class StaffDealRepaymentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_admin()
        cls.customer, cls.deal = make_customer_with_deal()
        _fund_and_select_quote(cls.deal)

    def setUp(self):
        self.client.force_login(self.staff)

    def _get(self):
        return self.client.get(reverse("crm:deal_detail", args=[self.deal.pk]))

    def test_schedule_renders_on_deal_detail(self):
        response = self._get()
        self.assertContains(response, "Repayments")
        self.assertContains(response, "1 Aug 2026")   # first payment due
        self.assertContains(response, "1 Jul 2031")   # 60th and last
        self.assertContains(response, "526.79")       # the monthly payment

    def test_hint_when_no_quote_selected(self):
        self.deal.selected_quote = None
        self.deal.save(update_fields=["selected_quote"])
        response = self._get()
        self.assertContains(response, "Select a priced quote")
        # The header still shows "First payment: 1 Aug 2026" — assert on a
        # date only the schedule table would contain.
        self.assertNotContains(response, "1 Jul 2031")

    def test_no_section_without_first_payment_date(self):
        self.deal.first_payment_date = None
        self.deal.save(update_fields=["first_payment_date"])
        response = self._get()
        self.assertNotContains(response, "Repayments")


class PortalRepaymentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.deal = make_customer_with_deal()
        _fund_and_select_quote(cls.deal)

    def setUp(self):
        self.client.force_login(self.user)

    def _get(self):
        return self.client.get(
            reverse("crm:portal_application_complete", args=[self.deal.pk])
        )

    def test_schedule_renders_on_application_complete(self):
        response = self._get()
        self.assertContains(response, "Repayments")
        self.assertContains(response, "1 Aug 2026")
        self.assertContains(response, "1 Jul 2031")
        self.assertContains(response, "526.79")

    def test_no_schedule_without_first_payment_date(self):
        self.deal.first_payment_date = None
        self.deal.save(update_fields=["first_payment_date"])
        response = self._get()
        self.assertNotContains(response, "Repayments")
