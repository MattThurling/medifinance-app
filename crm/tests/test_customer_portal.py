"""The /my/ customer portal — shell, scoping, and inline HTMX editing.

The HTMX contract under test: card partial → edit partial → save returns the
card partial (or errors in place); every save that changed something logs one
customer_update Note; everything is scoped to the logged-in customer."""

from django.core.exceptions import PermissionDenied  # noqa: F401 (documentation)
from django.test import TestCase
from django.urls import reverse

from crm.models import Note

from .factories import (
    make_associate,
    make_contact,
    make_customer_with_deal,
    make_document,
    make_quote,
)

HTMX = {"HTTP_HX_REQUEST": "true"}


def company_form_data(org, **overrides):
    """Valid POST payload for the company edit form (prefix org<pk>)."""
    p = f"org{org.pk}"
    data = {
        f"{p}-address_line1": org.address_line1,
        f"{p}-address_line2": org.address_line2,
        f"{p}-address_city": org.address_city,
        f"{p}-address_county": org.address_county,
        f"{p}-address_postcode": org.address_postcode,
        f"{p}-phone": org.phone,
        f"{p}-email": org.email,
        f"{p}-url": org.url,
    }
    data.update({f"{p}-{k}": v for k, v in overrides.items()})
    return data


def person_form_data(person, **overrides):
    p = f"person{person.pk}"
    data = {
        f"{p}-first_name": person.first_name,
        f"{p}-last_name": person.last_name,
        f"{p}-phone": person.phone,
        f"{p}-home_address_line1": person.home_address_line1,
        f"{p}-home_address_line2": person.home_address_line2,
        f"{p}-home_address_city": person.home_address_city,
        f"{p}-home_address_county": person.home_address_county,
        f"{p}-home_address_postcode": person.home_address_postcode,
    }
    data.update({f"{p}-{k}": v for k, v in overrides.items()})
    return data


class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def _urls(self):
        return [
            reverse("crm:my_company"),
            reverse("crm:my_people"),
            reverse("crm:my_deals"),
            reverse("crm:my_deal_detail", args=[self.deal.pk]),
            reverse("crm:my_company_edit", args=[self.deal.organisation_id]),
        ]

    def test_anonymous_blocked(self):
        for url in self._urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_staff_blocked(self):
        self.client.force_login(make_associate())
        for url in self._urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_customer_pages_render_with_nav(self):
        self.client.force_login(self.customer)
        for url in [reverse("crm:my_company"), reverse("crm:my_people"),
                    reverse("crm:my_deals"), reverse("crm:my_deal_detail", args=[self.deal.pk])]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Your account")  # customer nav title
                # No staff nav: the exact hrefs staff items render with.
                content = response.content.decode()
                self.assertNotIn('href="/deals/"', content)
                self.assertNotIn('href="/contacts/"', content)
                self.assertNotIn('href="/organisations/"', content)

    def test_customer_dashboard_shows_portal_cards(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, reverse("crm:my_company"))
        self.assertContains(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))
        # The wizard stays reachable.
        self.assertContains(response, reverse("crm:portal_quote_select", args=[self.deal.pk]))


class IsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer_a, cls.deal_a = make_customer_with_deal()
        cls.customer_b, cls.deal_b = make_customer_with_deal()

    def test_foreign_records_404(self):
        self.client.force_login(self.customer_a)
        for url in [
            reverse("crm:my_deal_detail", args=[self.deal_b.pk]),
            reverse("crm:my_company_card", args=[self.deal_b.organisation_id]),
            reverse("crm:my_company_edit", args=[self.deal_b.organisation_id]),
            reverse("crm:my_person_edit", args=[self.deal_b.customer_id]),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, **HTMX).status_code, 404)

    def test_foreign_post_does_not_mutate(self):
        self.client.force_login(self.customer_a)
        org_b = self.deal_b.organisation
        original = org_b.address_line1
        response = self.client.post(
            reverse("crm:my_company_edit", args=[org_b.pk]),
            company_form_data(org_b, address_line1="HIJACKED"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 404)
        org_b.refresh_from_db()
        self.assertEqual(org_b.address_line1, original)

    def test_lists_scoped(self):
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("crm:my_deals"))
        self.assertContains(response, self.deal_a.name)
        self.assertNotContains(response, self.deal_b.name)

    def test_org_colleagues_not_in_people(self):
        """Another contact at the customer's own organisation is NOT theirs to see."""
        colleague = make_contact(organisation=self.deal_a.organisation,
                                 first_name="Colleague", last_name="Hidden")
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("crm:my_people"))
        self.assertNotContains(response, "Colleague Hidden")

    def test_co_applicants_are_in_people(self):
        co = make_contact(first_name="Co", last_name="Applicant")
        self.deal_a.co_applicants.add(co)
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("crm:my_people"))
        self.assertContains(response, "Co Applicant")


class InlineEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        cls.org = cls.deal.organisation
        cls.contact = cls.deal.customer

    def setUp(self):
        self.client.force_login(self.customer)

    def test_edit_get_returns_form_fragment(self):
        response = self.client.get(
            reverse("crm:my_company_edit", args=[self.org.pk]), **HTMX
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<html", content)  # fragment, not a full page
        self.assertIn(f"org{self.org.pk}-address_line1", content)
        self.assertIn("hx-post", content)

    def test_valid_post_saves_and_returns_card_with_note(self):
        response = self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            company_form_data(self.org, address_line1="42 New Street", phone="0117 123456"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("42 New Street", content)
        self.assertNotIn("hx-post", content)  # back to display card

        self.org.refresh_from_db()
        self.assertEqual(self.org.address_line1, "42 New Street")

        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.organisation, self.org)
        self.assertEqual(note.author, self.customer)
        self.assertIn("company details", note.content)
        self.assertIn("Line 1", note.content)
        self.assertIn("Phone", note.content)

    def test_unchanged_post_creates_no_note(self):
        self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            company_form_data(self.org),
            **HTMX,
        )
        self.assertEqual(Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).count(), 0)

    def test_invalid_post_returns_form_no_note(self):
        response = self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            company_form_data(self.org, email="not-an-email"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("hx-post", content)  # still the form
        self.assertIn("valid email", content)
        self.assertEqual(Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).count(), 0)

    def test_name_and_ch_number_locked(self):
        original_name = self.org.name
        p = f"org{self.org.pk}"
        self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            {**company_form_data(self.org, address_line1="1 Lock Test"),
             f"{p}-name": "HIJACKED LTD",
             f"{p}-companies_house_number": "99999999"},
            **HTMX,
        )
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, original_name)
        self.assertEqual(self.org.companies_house_number, "")
        self.assertEqual(self.org.address_line1, "1 Lock Test")

    def test_person_edit_saves_with_note_on_contact(self):
        response = self.client.post(
            reverse("crm:my_person_edit", args=[self.contact.pk]),
            person_form_data(self.contact, phone="07700 900123"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.phone, "07700 900123")
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.contact, self.contact)
        self.assertIn("personal details", note.content)

    def test_card_get_returns_display_fragment(self):
        response = self.client.get(
            reverse("crm:my_company_card", args=[self.org.pk]), **HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("hx-post", response.content.decode())

    def test_non_htmx_get_redirects_to_page(self):
        response = self.client.get(reverse("crm:my_company_edit", args=[self.org.pk]))
        self.assertRedirects(response, reverse("crm:my_company"))

    def test_non_htmx_post_saves_and_redirects(self):
        response = self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            company_form_data(self.org, address_line1="99 Fallback Way"),
        )
        self.assertRedirects(response, reverse("crm:my_company"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.address_line1, "99 Fallback Way")


class DealReadOnlyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def setUp(self):
        self.client.force_login(self.customer)

    def test_detail_renders_without_quote_or_schedule(self):
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No quote has been chosen yet")

    def test_detail_shows_quote_and_documents_read_only(self):
        quote = make_quote(self.deal)
        self.deal.selected_quote = quote
        self.deal.save(update_fields=["selected_quote"])
        doc = make_document(self.deal, name="Bank statements")

        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, f"{quote.term} months")
        self.assertContains(response, "Bank statements")
        self.assertContains(response, "Requested")
        # Read-only: no upload controls, no forms beyond none at all.
        content = response.content.decode()
        self.assertNotIn("type=\"file\"", content)
        self.assertNotIn("document_upload", content)
