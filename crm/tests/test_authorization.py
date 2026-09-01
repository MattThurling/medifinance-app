"""Role-based authorization matrix.

The CRM has three roles (admin / associate / customer). Staff (admin + associate)
own the internal pages; customers are limited to the portal for their own deals.
This module asserts that hitting any staff URL as the wrong role gives 403, and
that mutating endpoints (delete) won't run for a customer.

Convention used here:
  * Anonymous + customer get **403** on staff URLs. `StaffRequiredMixin` raises
    PermissionDenied before `LoginRequiredMixin` has a chance to redirect — the
    role check runs first. If we ever want anon to redirect to login instead,
    fix `RoleRequiredMixin.dispatch` and update these tests."""

from django.test import TestCase
from django.urls import reverse

from crm.models import Deal, Document, Organisation, Participation, Proposal, Quote

from .factories import (
    make_admin,
    make_associate,
    make_customer,
    make_deal,
    make_document,
    make_participation,
    make_proposal,
    make_quote,
)


class StaffUrlAccessMatrixTests(TestCase):
    """Every staff URL, three roles, expected status. Captured as a list so a
    new URL added without a permission decorator gets caught the first time CI
    runs against it."""

    @classmethod
    def setUpTestData(cls):
        cls.associate = make_associate()
        cls.admin = make_admin()
        cls.customer = make_customer()
        cls.deal = make_deal(owner=cls.associate)
        cls.contact = cls.deal.customer
        cls.org = cls.deal.organisation
        cls.quote = make_quote(cls.deal)
        cls.proposal = make_proposal(cls.deal)
        cls.participation = make_participation(cls.deal)
        cls.document = make_document(cls.deal)

    def _staff_get_urls(self):
        d = self.deal.pk
        return [
            # Organisations
            reverse("crm:organisation_list"),
            reverse("crm:organisation_create"),
            reverse("crm:organisation_detail", args=[self.org.pk]),
            reverse("crm:organisation_update", args=[self.org.pk]),
            reverse("crm:organisation_delete", args=[self.org.pk]),
            # Contacts
            reverse("crm:contact_list"),
            reverse("crm:contact_create"),
            reverse("crm:contact_detail", args=[self.contact.pk]),
            reverse("crm:contact_update", args=[self.contact.pk]),
            reverse("crm:contact_delete", args=[self.contact.pk]),
            # Reports
            reverse("crm:reports"),
            # Deals
            reverse("crm:deal_list"),
            reverse("crm:deal_create"),
            reverse("crm:deal_detail", args=[d]),
            reverse("crm:deal_update", args=[d]),
            reverse("crm:deal_delete", args=[d]),
            # Sub-resources (require ?deal=)
            reverse("crm:quote_create") + f"?deal={d}",
            reverse("crm:quote_update", args=[self.quote.pk]),
            reverse("crm:quote_delete", args=[self.quote.pk]),
            reverse("crm:proposal_create") + f"?deal={d}",
            reverse("crm:proposal_update", args=[self.proposal.pk]),
            reverse("crm:participation_create") + f"?deal={d}",
            reverse("crm:participation_update", args=[self.participation.pk]),
            reverse("crm:document_create") + f"?deal={d}",
            # Rates + HTMX
            reverse("crm:rates"),
            reverse("crm:rate_band_add"),
            reverse("crm:rate_upload"),
            reverse("crm:contact_search"),
            reverse("crm:organisation_search"),
            reverse("crm:user_search"),
            reverse("crm:quote_rate_options"),
            # Xero URLs are NOT here — they need the finance flag on top of a
            # staff role; see FinanceUrlAccessMatrixTests below.
        ]

    def test_anonymous_blocked_from_staff_urls(self):
        for url in self._staff_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 403,
                    f"anon GET {url} expected 403, got {response.status_code}",
                )

    def test_customer_blocked_from_staff_urls(self):
        self.client.force_login(self.customer)
        for url in self._staff_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 403,
                    f"customer GET {url} expected 403, got {response.status_code}",
                )

    def test_associate_allowed_on_staff_urls(self):
        self.client.force_login(self.associate)
        for url in self._staff_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (200, 302),
                    f"associate GET {url} expected 200/302, got {response.status_code}",
                )

    def test_admin_allowed_on_staff_urls(self):
        self.client.force_login(self.admin)
        for url in self._staff_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (200, 302),
                    f"admin GET {url} expected 200/302, got {response.status_code}",
                )


class FinanceUrlAccessMatrixTests(TestCase):
    """The Xero integration needs `User.is_finance` on top of a staff role.
    Strict gating: an admin without the flag gets 403 like everyone else, and
    the nav item / deal-detail card render only for finance users."""

    @classmethod
    def setUpTestData(cls):
        cls.associate = make_associate()
        cls.admin = make_admin()
        cls.customer = make_customer()
        cls.finance_associate = make_associate(is_finance=True)
        cls.finance_admin = make_admin(is_finance=True)
        cls.deal = make_deal(owner=cls.associate)

    def _finance_get_urls(self):
        return [
            reverse("crm:xero_status"),
            reverse("crm:xero_connect"),
            reverse("crm:xero_callback"),
            reverse("crm:deal_raise_invoice", args=[self.deal.pk]),
        ]

    def _assert_all_403(self, label):
        for url in self._finance_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 403,
                    f"{label} GET {url} expected 403, got {response.status_code}",
                )
        # xero_disconnect + xero_sync are POST-only (GET would be a 405
        # regardless of role).
        for url_name in ("crm:xero_disconnect", "crm:xero_sync"):
            response = self.client.post(reverse(url_name))
            self.assertEqual(response.status_code, 403, f"{label} POST {url_name}")

    def _assert_all_allowed(self, label):
        # 302s are legitimate: without creds/connection/state the views
        # redirect back to the status page with a message.
        for url in self._finance_get_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (200, 302),
                    f"{label} GET {url} expected 200/302, got {response.status_code}",
                )
        for url_name in ("crm:xero_disconnect", "crm:xero_sync"):
            response = self.client.post(reverse(url_name))
            self.assertIn(response.status_code, (200, 302), f"{label} POST {url_name}")

    def test_anonymous_blocked(self):
        self._assert_all_403("anon")

    def test_customer_blocked(self):
        self.client.force_login(self.customer)
        self._assert_all_403("customer")

    def test_associate_without_flag_blocked(self):
        self.client.force_login(self.associate)
        self._assert_all_403("associate")

    def test_admin_without_flag_blocked(self):
        # The strict-gating regression test: role alone is never enough.
        self.client.force_login(self.admin)
        self._assert_all_403("admin")

    def test_finance_flagged_customer_still_blocked(self):
        # The flag only takes effect for staff roles.
        self.client.force_login(make_customer(is_finance=True))
        self._assert_all_403("finance-flagged customer")

    def test_finance_associate_allowed(self):
        self.client.force_login(self.finance_associate)
        self._assert_all_allowed("finance associate")

    def test_finance_admin_allowed(self):
        self.client.force_login(self.finance_admin)
        self._assert_all_allowed("finance admin")

    def test_deal_detail_hides_xero_card_from_non_finance_staff(self):
        self.client.force_login(self.associate)
        response = self.client.get(reverse("crm:deal_detail", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Xero invoices")
        self.assertNotContains(response, "Raise invoice")
        self.assertNotContains(response, reverse("crm:xero_status"))

    def test_deal_detail_shows_xero_card_to_finance_staff(self):
        self.client.force_login(self.finance_associate)
        response = self.client.get(reverse("crm:deal_detail", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Xero invoices")
        self.assertContains(response, "Raise invoice")
        self.assertContains(response, reverse("crm:xero_status"))


class CustomerCannotMutateStaffResourcesTests(TestCase):
    """Tighter check: hitting a destructive POST endpoint as a customer must
    not change DB state. A 403 GET is necessary but not sufficient — we also
    need the record to still exist after."""

    @classmethod
    def setUpTestData(cls):
        cls.associate = make_associate()
        cls.customer = make_customer()
        cls.deal = make_deal(owner=cls.associate)
        cls.quote = make_quote(cls.deal)
        cls.proposal = make_proposal(cls.deal)
        cls.participation = make_participation(cls.deal)
        cls.document = make_document(cls.deal)

    def setUp(self):
        self.client.force_login(self.customer)

    def test_customer_cannot_delete_deal(self):
        url = reverse("crm:deal_delete", args=[self.deal.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Deal.objects.filter(pk=self.deal.pk).exists())

    def test_customer_cannot_delete_organisation(self):
        url = reverse("crm:organisation_delete", args=[self.deal.organisation.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Organisation.objects.filter(pk=self.deal.organisation.pk).exists())

    def test_customer_cannot_delete_quote(self):
        url = reverse("crm:quote_delete", args=[self.quote.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Quote.objects.filter(pk=self.quote.pk).exists())

    def test_customer_cannot_delete_participation(self):
        url = reverse("crm:participation_delete", args=[self.participation.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Participation.objects.filter(pk=self.participation.pk).exists())

    def test_customer_cannot_delete_document(self):
        url = reverse("crm:document_delete", args=[self.document.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    def test_customer_cannot_edit_deal(self):
        url = reverse("crm:deal_update", args=[self.deal.pk])
        response = self.client.post(url, {"name": "PWNED"})
        self.assertEqual(response.status_code, 403)
        self.deal.refresh_from_db()
        self.assertNotEqual(self.deal.name, "PWNED")

    def test_customer_cannot_edit_proposal(self):
        url = reverse("crm:proposal_update", args=[self.proposal.pk])
        response = self.client.post(url, {"status": Proposal.Status.WITHDRAWN})
        self.assertEqual(response.status_code, 403)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, Proposal.Status.SUBMITTED)

    def test_customer_cannot_notify_client_of_proposal(self):
        url = reverse("crm:proposal_notify", args=[self.proposal.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.proposal.refresh_from_db()
        self.assertIsNone(self.proposal.notified_at)

    def test_customer_cannot_issue_portal_link_for_someone_elses_deal(self):
        url = reverse("crm:deal_issue_portal_link", args=[self.deal.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_raise_xero_invoice(self):
        url = reverse("crm:deal_raise_invoice", args=[self.deal.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_upload_rate_csv(self):
        url = reverse("crm:rate_upload")
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 403)


class StaffApiAccessTests(TestCase):
    """Extension JSON API returns JSON errors (not HTML redirects) so the
    extension can show a sensible message. Customers + anon get 401/403."""

    @classmethod
    def setUpTestData(cls):
        cls.associate = make_associate()
        cls.customer = make_customer()
        cls.deal = make_deal(owner=cls.associate)

    def test_anon_gets_401_json(self):
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_customer_gets_403_json(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"], "forbidden")

    def test_staff_gets_deal_list(self):
        self.client.force_login(self.associate)
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user"]["email"], self.associate.email)
        self.assertEqual(len(data["deals"]), 1)
        self.assertEqual(data["deals"][0]["id"], self.deal.pk)

    def test_customer_gets_403_on_deal_detail_api(self):
        self.client.force_login(self.customer)
        url = reverse("crm_api:deal_detail", args=[self.deal.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_anon_gets_401_on_deal_detail_api(self):
        url = reverse("crm_api:deal_detail", args=[self.deal.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)
