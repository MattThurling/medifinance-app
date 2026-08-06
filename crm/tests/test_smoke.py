"""Smoke tests — every key page renders without 500ing. Catches broken imports,
template syntax errors, missing context, URL conf regressions. Cheap to maintain
and the first thing CI tells you when something's badly broken."""

from django.test import TestCase
from django.urls import reverse

from .factories import (
    make_admin,
    make_associate,
    make_customer_with_deal,
    make_deal,
    make_document,
    make_participation,
    make_proposal,
    make_quote,
)


class PublicPageSmokeTests(TestCase):
    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_redirects_anonymous_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_developer_docs_render_anonymously(self):
        """Public dev page — must load for anyone, no login redirect."""
        response = self.client.get(reverse("developers"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "POST /api/deals/")
        self.assertContains(response, "POST /api/quotes/")
        self.assertContains(response, "Bearer")
        # The try-it widget should be present.
        self.assertContains(response, 'id="try-it-form"')


class StaffPagesRenderTests(TestCase):
    """Every CRM list/detail/create/update page should GET cleanly for staff.

    This is the heavy smoke test — most regressions surface here as template
    or context errors before authorization tests run."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)
        cls.contact = cls.deal.customer
        cls.org = cls.deal.organisation
        cls.quote = make_quote(cls.deal)
        cls.proposal = make_proposal(cls.deal)
        cls.participation = make_participation(cls.deal)
        cls.document = make_document(cls.deal)

    def setUp(self):
        self.client.force_login(self.staff)

    def _assert_get_ok(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"GET {url} returned {response.status_code}")

    def test_dashboard(self):
        self._assert_get_ok(reverse("dashboard"))

    def test_organisation_pages(self):
        self._assert_get_ok(reverse("crm:organisation_list"))
        self._assert_get_ok(reverse("crm:organisation_create"))
        self._assert_get_ok(reverse("crm:organisation_detail", args=[self.org.pk]))
        self._assert_get_ok(reverse("crm:organisation_update", args=[self.org.pk]))
        self._assert_get_ok(reverse("crm:organisation_delete", args=[self.org.pk]))

    def test_contact_pages(self):
        self._assert_get_ok(reverse("crm:contact_list"))
        self._assert_get_ok(reverse("crm:contact_create"))
        self._assert_get_ok(reverse("crm:contact_detail", args=[self.contact.pk]))
        self._assert_get_ok(reverse("crm:contact_update", args=[self.contact.pk]))
        self._assert_get_ok(reverse("crm:contact_delete", args=[self.contact.pk]))

    def test_deal_pages(self):
        self._assert_get_ok(reverse("crm:deal_list"))
        self._assert_get_ok(reverse("crm:deal_create"))
        self._assert_get_ok(reverse("crm:deal_detail", args=[self.deal.pk]))
        self._assert_get_ok(reverse("crm:deal_update", args=[self.deal.pk]))
        self._assert_get_ok(reverse("crm:deal_delete", args=[self.deal.pk]))

    def test_deal_detail_links_hubspot_record(self):
        self.deal.hubspot_id = "123456789"
        self.deal.save(update_fields=["hubspot_id"])
        response = self.client.get(reverse("crm:deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "record/0-3/123456789")

    def test_quote_proposal_participation_create_pages(self):
        self._assert_get_ok(reverse("crm:quote_create") + f"?deal={self.deal.pk}")
        self._assert_get_ok(reverse("crm:quote_update", args=[self.quote.pk]))
        self._assert_get_ok(reverse("crm:quote_delete", args=[self.quote.pk]))
        self._assert_get_ok(reverse("crm:proposal_create") + f"?deal={self.deal.pk}")
        self._assert_get_ok(reverse("crm:proposal_update", args=[self.proposal.pk]))
        self._assert_get_ok(reverse("crm:participation_create") + f"?deal={self.deal.pk}")
        self._assert_get_ok(reverse("crm:participation_update", args=[self.participation.pk]))

    def test_document_pages(self):
        self._assert_get_ok(reverse("crm:document_create") + f"?deal={self.deal.pk}")

    def test_rate_pages(self):
        self._assert_get_ok(reverse("crm:rates"))
        self._assert_get_ok(reverse("crm:rate_band_add"))
        self._assert_get_ok(reverse("crm:rate_upload"))

    def test_xero_status_page(self):
        self._assert_get_ok(reverse("crm:xero_status"))


class PortalPagesRenderTests(TestCase):
    """The customer-facing wizard pages render for their owner."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.deal = make_customer_with_deal()
        cls.quote = make_quote(cls.deal)
        # Pre-select a quote so steps after Quote-select don't bounce.
        cls.deal.selected_quote = cls.quote
        cls.deal.save(update_fields=["selected_quote"])

    def setUp(self):
        self.client.force_login(self.user)

    def _ok(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"GET {url} returned {response.status_code}")

    def test_portal_steps(self):
        self._ok(reverse("crm:portal_quote_select", args=[self.deal.pk]))
        self._ok(reverse("crm:portal_company", args=[self.deal.pk]))
        self._ok(reverse("crm:portal_applicants", args=[self.deal.pk]))
        self._ok(reverse("crm:portal_documents", args=[self.deal.pk]))
        self._ok(reverse("crm:portal_application_complete", args=[self.deal.pk]))

    def test_customer_dashboard(self):
        self._ok(reverse("dashboard"))


class AdminCanRenderEverythingTest(TestCase):
    """Admins are a superset of associates — make sure they aren't blocked
    anywhere associates can go."""

    def test_admin_can_load_deal_list(self):
        admin = make_admin()
        self.client.force_login(admin)
        response = self.client.get(reverse("crm:deal_list"))
        self.assertEqual(response.status_code, 200)
