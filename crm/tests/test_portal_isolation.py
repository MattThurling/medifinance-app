"""Customer-portal isolation — the security boundary that matters most.

A customer on deal A must not be able to see, edit, or alter anything on
deal B. The portal views run their queries with `customer__user=request.user`
so cross-deal access returns 404 (the deal is invisible to the wrong user)."""

from django.test import TestCase
from django.urls import reverse

from crm.models import Stage

from .factories import (
    make_associate,
    make_customer_with_deal,
    make_quote,
)


class PortalDealIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer_a, cls.deal_a = make_customer_with_deal()
        cls.customer_b, cls.deal_b = make_customer_with_deal()
        cls.quote_a = make_quote(cls.deal_a)
        cls.quote_b = make_quote(cls.deal_b)
        # Pre-select a quote on both so step-gating doesn't redirect to step 1.
        cls.deal_a.selected_quote = cls.quote_a
        cls.deal_a.save(update_fields=["selected_quote"])
        cls.deal_b.selected_quote = cls.quote_b
        cls.deal_b.save(update_fields=["selected_quote"])

    def _portal_urls(self, deal_pk):
        return [
            reverse("crm:portal_quote_select", args=[deal_pk]),
            reverse("crm:portal_company", args=[deal_pk]),
            reverse("crm:portal_applicants", args=[deal_pk]),
            reverse("crm:portal_documents", args=[deal_pk]),
            reverse("crm:portal_application_complete", args=[deal_pk]),
        ]

    def test_customer_can_access_own_portal_pages(self):
        self.client.force_login(self.customer_a)
        for url in self._portal_urls(self.deal_a.pk):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, f"GET {url} → {response.status_code}")

    def test_customer_cannot_see_other_customers_portal_pages(self):
        """The deal lookup is `customer__user=request.user`, so foreign-deal
        access returns 404 — the deal simply doesn't exist for this user."""
        self.client.force_login(self.customer_a)
        for url in self._portal_urls(self.deal_b.pk):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404, f"GET {url} → {response.status_code}")

    def test_staff_blocked_from_portal_pages(self):
        """Staff get 403 — the portal is the customer wizard, not for staff."""
        self.client.force_login(make_associate())
        for url in self._portal_urls(self.deal_a.pk):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403, f"GET {url} → {response.status_code}")

    def test_anonymous_blocked_from_portal_pages(self):
        for url in self._portal_urls(self.deal_a.pk):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403, f"GET {url} → {response.status_code}")


class PortalCrossCustomerMutationTests(TestCase):
    """Going beyond GET: a customer POSTing to another customer's portal must
    not mutate the target deal. The 404 isn't enough on its own — confirm DB
    state is untouched."""

    @classmethod
    def setUpTestData(cls):
        cls.customer_a, cls.deal_a = make_customer_with_deal()
        cls.customer_b, cls.deal_b = make_customer_with_deal()
        cls.quote_b = make_quote(cls.deal_b)

    def test_customer_a_cannot_select_quote_on_deal_b(self):
        self.client.force_login(self.customer_a)
        url = reverse("crm:portal_quote_select", args=[self.deal_b.pk])
        response = self.client.post(url, {"quote": self.quote_b.pk})
        self.assertEqual(response.status_code, 404)
        self.deal_b.refresh_from_db()
        self.assertIsNone(self.deal_b.selected_quote_id)

    def test_customer_a_cannot_update_company_on_deal_b(self):
        self.client.force_login(self.customer_a)
        url = reverse("crm:portal_company", args=[self.deal_b.pk])
        original_name = self.deal_b.organisation.name
        response = self.client.post(url, {
            "company-name": "HIJACKED",
            "company-address_line1": "1 Fake St",
        })
        self.assertEqual(response.status_code, 404)
        self.deal_b.organisation.refresh_from_db()
        self.assertEqual(self.deal_b.organisation.name, original_name)

    def test_customer_a_cannot_advance_stage_on_deal_b(self):
        """The PortalApplicants POST records a Stage on success. A foreign
        customer hitting this URL should not create any stage event on deal_b."""
        self.client.force_login(self.customer_a)
        starting_stages = Stage.objects.filter(deal=self.deal_b).count()
        url = reverse("crm:portal_applicants", args=[self.deal_b.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Stage.objects.filter(deal=self.deal_b).count(), starting_stages)
