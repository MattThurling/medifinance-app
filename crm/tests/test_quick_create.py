"""Inline "+ New" beside the Customer / Organisation comboboxes on the deal
form and the Supplier combobox on the participation form.

The HTMX contract under test (``crm.views.shared._QuickCreateView``): an
HTMX GET returns the slim form partial, a plain GET redirects to the full
create page, an invalid POST re-renders the partial with errors, and a valid
POST answers 204 + ``HX-Trigger {"quickCreated": {id, label}}`` which the
page JS turns into the selected record."""

import json

from django.test import TestCase
from django.urls import reverse

from crm.models import Contact, Deal, Organisation

from .factories import make_admin, make_associate, make_customer, make_deal, make_organisation

HTMX = {"HTTP_HX_REQUEST": "true"}


def _trigger(response):
    return json.loads(response["HX-Trigger"])


class OrganisationQuickCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.url = reverse("crm:organisation_quick_create")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_hx_get_returns_partial(self):
        response = self.client.get(self.url, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "crm/_quick_create_form.html")
        self.assertContains(response, 'hx-post="/organisations/quick-new/"')
        self.assertContains(response, "New organisation")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertNotContains(response, "<html")
        # Owner is never offered inline — the creator owns the record.
        self.assertNotContains(response, 'name="owner"')

    def test_plain_get_redirects_to_full_form(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("crm:organisation_create"), fetch_redirect_response=False)

    def test_post_valid_creates_record_and_returns_trigger(self):
        response = self.client.post(self.url, {"name": "Acme Dental", "sector": ""}, **HTMX)
        self.assertEqual(response.status_code, 204)
        org = Organisation.objects.get(name="Acme Dental")
        self.assertEqual(org.owner, self.admin)
        self.assertEqual(_trigger(response), {"quickCreated": {"id": org.pk, "label": "Acme Dental"}})

    def test_post_ignores_forged_owner(self):
        other = make_associate()
        self.client.post(self.url, {"name": "Forged", "owner": other.pk}, **HTMX)
        self.assertEqual(Organisation.objects.get(name="Forged").owner, self.admin)

    def test_post_invalid_rerenders_with_errors(self):
        response = self.client.post(self.url, {"name": ""}, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "crm/_quick_create_form.html")
        self.assertContains(response, "This field is required")
        self.assertFalse(Organisation.objects.exists())

    def test_associate_without_organisation_owns_what_they_create(self):
        associate = make_associate()
        self.client.force_login(associate)
        response = self.client.post(self.url, {"name": "Mine"}, **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(Organisation.objects.get(name="Mine").owner, associate)


class ContactQuickCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.org = make_organisation(name="Linked Ltd")
        cls.url = reverse("crm:contact_quick_create")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_hx_get_returns_partial(self):
        response = self.client.get(self.url, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "crm/_quick_create_form.html")
        self.assertContains(response, 'hx-post="/contacts/quick-new/"')
        self.assertContains(response, "New contact")
        self.assertNotContains(response, "Will be linked to")

    def test_plain_get_redirects_keeping_query(self):
        response = self.client.get(self.url + f"?organisation={self.org.pk}")
        self.assertRedirects(
            response,
            reverse("crm:contact_create") + f"?organisation={self.org.pk}",
            fetch_redirect_response=False,
        )

    def test_hx_get_prefills_hidden_organisation(self):
        response = self.client.get(self.url, {"organisation": self.org.pk}, **HTMX)
        self.assertContains(response, "Will be linked to Linked Ltd")
        self.assertContains(
            response, f'<input type="hidden" name="organisations" value="{self.org.pk}"', html=False
        )

    def test_bad_or_empty_organisation_param_is_ignored(self):
        for value in ("", "abc", "999999"):
            response = self.client.get(self.url, {"organisation": value}, **HTMX)
            self.assertEqual(response.status_code, 200, value)
            self.assertNotContains(response, "Will be linked to")

    def test_post_valid_links_organisation_and_returns_trigger(self):
        response = self.client.post(
            self.url,
            {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
             "organisations": [self.org.pk]},
            **HTMX,
        )
        self.assertEqual(response.status_code, 204)
        contact = Contact.objects.get(email="jane@example.com")
        self.assertEqual(contact.owner, self.admin)
        self.assertEqual(list(contact.organisations.all()), [self.org])
        self.assertEqual(_trigger(response), {"quickCreated": {"id": contact.pk, "label": "Jane Doe"}})

    def test_post_invalid_rerenders_with_errors(self):
        response = self.client.post(self.url, {"email": "not-an-email"}, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email address")
        self.assertFalse(Contact.objects.exists())

    def test_associate_without_org_can_use_new_contact_as_deal_customer(self):
        associate = make_associate()
        self.client.force_login(associate)
        response = self.client.post(
            self.url, {"first_name": "New", "last_name": "Customer"}, **HTMX
        )
        self.assertEqual(response.status_code, 204)
        contact = Contact.objects.get(first_name="New")
        self.assertEqual(contact.owner, associate)
        response = self.client.post(
            reverse("crm:deal_create"),
            {"name": "Inline deal", "customer": contact.pk, "organisation": self.org.pk},
        )
        deal = Deal.objects.get(name="Inline deal")
        self.assertRedirects(response, deal.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(deal.customer, contact)
        self.assertEqual(deal.organisation, self.org)


class QuickCreateAccessTests(TestCase):
    URLS = ["crm:organisation_quick_create", "crm:contact_quick_create"]

    def test_anonymous_forbidden(self):
        for name in self.URLS:
            url = reverse(name)
            self.assertEqual(self.client.get(url, **HTMX).status_code, 403, name)
            self.assertEqual(self.client.post(url, {"name": "x"}, **HTMX).status_code, 403, name)
        self.assertFalse(Organisation.objects.exists())

    def test_customer_forbidden(self):
        self.client.force_login(make_customer())
        for name in self.URLS:
            url = reverse(name)
            self.assertEqual(self.client.get(url, **HTMX).status_code, 403, name)
            self.assertEqual(self.client.post(url, {"name": "x"}, **HTMX).status_code, 403, name)
        self.assertFalse(Organisation.objects.exists())


class QuickCreateButtonsOnFormsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.contact_url = reverse("crm:contact_quick_create")
        cls.org_url = reverse("crm:organisation_quick_create")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_deal_form_has_both_buttons_and_dialogs(self):
        response = self.client.get(reverse("crm:deal_create"))
        self.assertContains(response, 'hx-get="' + self.contact_url + '"')
        self.assertContains(response, 'hx-get="' + self.org_url + '"')
        self.assertContains(response, 'id="quick-create-customer"')
        self.assertContains(response, 'id="quick-create-organisation"')
        # The new-contact GET carries the organisation currently picked.
        self.assertContains(response, """hx-include="[name='organisation']\"""")
        # Owner / introducer pickers are not quick-creatable.
        self.assertNotContains(response, 'id="quick-create-owner"')
        self.assertNotContains(response, 'id="quick-create-introducer"')

    def test_deal_form_with_locked_customer_only_offers_organisation(self):
        deal = make_deal(owner=self.admin)
        response = self.client.get(reverse("crm:deal_create") + f"?customer={deal.customer.pk}")
        self.assertNotContains(response, 'id="quick-create-customer"')
        self.assertNotContains(response, 'hx-get="' + self.contact_url + '"')
        self.assertContains(response, 'id="quick-create-organisation"')

    def test_deal_edit_form_has_buttons(self):
        deal = make_deal(owner=self.admin)
        response = self.client.get(reverse("crm:deal_update", args=[deal.pk]))
        self.assertContains(response, 'id="quick-create-customer"')
        self.assertContains(response, 'id="quick-create-organisation"')

    def test_supplier_form_on_asset_finance_deal_has_button(self):
        deal = make_deal(owner=self.admin, type=Deal.Type.ASSET_FINANCE)
        response = self.client.get(reverse("crm:participation_create") + f"?deal={deal.pk}")
        self.assertContains(response, 'hx-get="' + self.org_url + '"')
        self.assertContains(response, 'id="quick-create-organisation"')

    def test_funded_amount_form_on_commercial_deal_has_no_button(self):
        deal = make_deal(owner=self.admin, type=Deal.Type.BUSINESS_LOANS)
        response = self.client.get(reverse("crm:participation_create") + f"?deal={deal.pk}")
        self.assertNotContains(response, "quick-create-organisation")
        self.assertNotContains(response, "+ New")
