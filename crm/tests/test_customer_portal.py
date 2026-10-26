"""The /my/ customer portal — shell, scoping, and inline HTMX editing.

The HTMX contract under test: card partial → edit partial → save returns the
card partial (or errors in place); every save that changed something logs one
customer_update Note; everything is scoped to the logged-in customer."""

from django.core.exceptions import PermissionDenied  # noqa: F401 (documentation)
from django.test import TestCase
from django.urls import reverse

from django.conf import settings
from django.core import mail

from accounts.models import MagicLink
from crm.models import Contact, Deal, DealCoApplicant, Note
from crm.portal_links import ContactNotOnDealError, issue_portal_link_for_deal
from crm.views.customer import customer_people

from .factories import (
    make_associate,
    make_co_applicant,
    make_contact,
    make_customer,
    make_customer_with_deal,
    make_deal,
    make_document,
    make_quote,
)

HTMX = {"HTTP_HX_REQUEST": "true"}


def complete_details(deal):
    """Fill in every key field the checklist looks at, so a deal starts with
    nothing outstanding on the people/company side."""
    for person in [deal.customer, *deal.co_applicants.all()]:
        person.date_of_birth = "1980-01-01"
        person.phone = "01234 567890"
        person.home_address_line1 = "1 Test Street"
        person.home_address_postcode = "AB1 2CD"
        person.save()
    org = deal.organisation
    if org is not None:
        org.address_line1 = "2 Trade Road"
        org.address_postcode = "AB1 2CD"
        org.phone = "01234 000000"
        org.save()


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
        f"{p}-expected_updated_at": org.updated_at.isoformat(),
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
        f"{p}-expected_updated_at": person.updated_at.isoformat(),
    }
    data.update({f"{p}-{k}": v for k, v in overrides.items()})
    return data


class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def _urls(self):
        return [
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
        for url in [reverse("crm:my_deals"), reverse("crm:my_deal_detail", args=[self.deal.pk])]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Your deals")  # the one customer nav item
                # No staff nav: the exact hrefs staff items render with.
                content = response.content.decode()
                self.assertNotIn('href="/deals/"', content)
                self.assertNotIn('href="/contacts/"', content)
                self.assertNotIn('href="/organisations/"', content)

    def test_root_routes_single_deal_customer_to_deal_page(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))

    def test_root_routes_multi_deal_customer_to_list(self):
        make_deal(customer=self.deal.customer, organisation=self.deal.organisation)
        self.client.force_login(self.customer)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("crm:my_deals"))

    def test_root_routes_customer_without_deals_to_list(self):
        self.client.force_login(make_customer())
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("crm:my_deals"))

    def test_old_company_and_people_pages_redirect_home(self):
        self.client.force_login(self.customer)
        for path in ["/my/company/", "/my/people/"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertRedirects(response, reverse("dashboard"), target_status_code=302)

    def test_magic_link_lands_on_the_deal_page(self):
        link, _warning = issue_portal_link_for_deal(self.deal)
        self.assertEqual(link.redirect_url, reverse("crm:my_deal_detail", args=[self.deal.pk]))
        response = self.client.get(reverse("consume_magic_link", args=[link.token]))
        self.assertRedirects(response, link.redirect_url)
        self.assertTrue(MagicLink.objects.get(pk=link.pk).is_consumed)

    def test_root_routes_co_applicant_to_their_deal(self):
        co = make_co_applicant(self.deal, with_user=True)
        self.client.force_login(co.user)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))

    def test_magic_link_for_co_applicant_creates_their_login_and_lands_on_the_deal(self):
        co = make_co_applicant(self.deal, email="co@example.com")
        link, warning = issue_portal_link_for_deal(self.deal, contact=co)
        self.assertIsNone(warning)
        co.refresh_from_db()
        self.assertEqual(co.user.email, "co@example.com")
        self.assertTrue(co.user.is_customer)
        self.assertEqual(link.user, co.user)

        response = self.client.get(reverse("consume_magic_link", args=[link.token]))
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))
        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse("crm:my_person_edit", args=[co.pk]))
        self.assertNotContains(page, reverse("crm:my_person_edit", args=[self.deal.customer_id]))

    def test_magic_link_for_contact_not_on_the_deal_is_refused(self):
        stranger = make_contact()
        with self.assertRaises(ContactNotOnDealError):
            issue_portal_link_for_deal(self.deal, contact=stranger)
        self.assertFalse(MagicLink.objects.exists())

    def test_co_applicant_sharing_the_primarys_login_email_gets_a_warning_not_a_relink(self):
        # Contact emails aren't unique; the login (User) email is. A new
        # applicant entered with an address that already has a login is
        # issued a link for that login, but the contact isn't re-linked.
        co = make_co_applicant(self.deal, email=self.customer.email)
        link, warning = issue_portal_link_for_deal(self.deal, contact=co)
        self.assertIsNotNone(warning)
        self.assertEqual(link.user, self.customer)  # the existing login
        co.refresh_from_db()
        self.assertIsNone(co.user)


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
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal_a.pk]))
        self.assertNotContains(response, "Colleague Hidden")

    def test_co_applicants_are_in_people(self):
        co = make_contact(first_name="Co", last_name="Applicant")
        self.deal_a.co_applicants.add(co)
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal_a.pk]))
        self.assertContains(response, "Co Applicant")
        self.assertContains(response, reverse("crm:my_person_edit", args=[co.pk]))

    def test_deal_page_shows_only_that_deals_people(self):
        """A co-applicant on deal A must not appear on the customer's deal B."""
        co = make_contact(first_name="Only", last_name="OnA")
        self.deal_a.co_applicants.add(co)
        deal_other = make_deal(customer=self.deal_a.customer, organisation=self.deal_a.organisation)
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("crm:my_deal_detail", args=[deal_other.pk]))
        self.assertNotContains(response, "Only OnA")
        self.assertContains(response, reverse("crm:my_person_edit", args=[self.deal_a.customer_id]))

    def test_co_applicant_with_login_sees_the_deal_with_a_restricted_view(self):
        primary = self.deal_a.customer
        primary.first_name, primary.last_name, primary.phone = "Pat", "Primary", "01234 567890"
        primary.save()
        co = make_co_applicant(self.deal_a, with_user=True, first_name="Chris", last_name="Co")
        self.client.force_login(co.user)

        response = self.client.get(reverse("crm:my_deals"))
        self.assertContains(response, self.deal_a.name)
        self.assertNotContains(response, self.deal_b.name)

        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal_a.pk]))
        self.assertEqual(page.status_code, 200)
        # Their own card is editable; the primary is named but shows no
        # personal data and no Edit; company, quote and applicant controls
        # belong to the primary.
        self.assertContains(page, reverse("crm:my_person_edit", args=[co.pk]))
        self.assertContains(page, "Pat Primary")
        self.assertNotContains(page, "01234 567890")
        self.assertNotContains(page, reverse("crm:my_person_edit", args=[primary.pk]))
        self.assertNotContains(page, reverse("crm:my_company_edit", args=[self.deal_a.organisation_id]))
        self.assertContains(page, "The lead applicant manages the company details.")
        self.assertNotContains(page, reverse("crm:my_deal_applicant_add", args=[self.deal_a.pk]))

    def test_co_applicant_cannot_reach_company_or_other_peoples_edit_endpoints(self):
        co = make_co_applicant(self.deal_a, with_user=True)
        self.client.force_login(co.user)
        for url in [
            reverse("crm:my_company_card", args=[self.deal_a.organisation_id]),
            reverse("crm:my_company_edit", args=[self.deal_a.organisation_id]),
            reverse("crm:my_person_card", args=[self.deal_a.customer_id]),
            reverse("crm:my_person_edit", args=[self.deal_a.customer_id]),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, **HTMX).status_code, 404)

        primary = self.deal_a.customer
        response = self.client.post(
            reverse("crm:my_person_edit", args=[primary.pk]),
            person_form_data(primary, phone="HIJACKED"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 404)
        primary.refresh_from_db()
        self.assertNotEqual(primary.phone, "HIJACKED")
        self.assertEqual(list(customer_people(co.user)), [co])

    def test_co_applicant_edits_their_own_details(self):
        co = make_co_applicant(self.deal_a, with_user=True)
        self.client.force_login(co.user)
        response = self.client.post(
            reverse("crm:my_person_edit", args=[co.pk]),
            person_form_data(co, phone="07700 900999"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        co.refresh_from_db()
        self.assertEqual(co.phone, "07700 900999")
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.author, co.user)
        self.assertEqual(note.contact, co)
        self.assertIn(co.user.full_name, note.content)


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

    def test_note_names_the_actor(self):
        self.client.post(
            reverse("crm:my_person_edit", args=[self.contact.pk]),
            person_form_data(self.contact, phone="07700 900123"),
            **HTMX,
        )
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertTrue(note.content.startswith(self.customer.full_name))
        self.assertNotIn("Customer updated", note.content)

    def test_stale_edit_is_rejected_and_the_form_rerenders_with_a_fresh_stamp(self):
        from datetime import timedelta
        from django.utils import timezone

        data = person_form_data(self.contact, phone="07700 900123")  # stamp = as rendered
        # Someone else saves in between.
        Contact.objects.filter(pk=self.contact.pk).update(
            phone="0117 000000", updated_at=timezone.now() + timedelta(seconds=5),
        )
        response = self.client.post(
            reverse("crm:my_person_edit", args=[self.contact.pk]), data, **HTMX
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("hx-post", content)  # still the form
        self.assertIn("Someone else changed these details", content)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.phone, "0117 000000")  # nothing saved
        self.assertEqual(Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).count(), 0)
        # The re-rendered form carries the current stamp, so a retry goes through.
        self.assertIn(self.contact.updated_at.isoformat(), content)
        response = self.client.post(
            reverse("crm:my_person_edit", args=[self.contact.pk]),
            person_form_data(self.contact, phone="07700 900123"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("hx-post", response.content.decode())
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.phone, "07700 900123")

    def test_stale_company_edit_is_rejected(self):
        from datetime import timedelta
        from django.utils import timezone

        data = company_form_data(self.org, address_line1="42 New Street")
        from crm.models import Organisation
        Organisation.objects.filter(pk=self.org.pk).update(
            address_line1="1 Other Way", updated_at=timezone.now() + timedelta(seconds=5),
        )
        response = self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]), data, **HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Someone else changed these details", response.content.decode())
        self.org.refresh_from_db()
        self.assertEqual(self.org.address_line1, "1 Other Way")

    def test_missing_stamp_is_tolerated(self):
        p = f"person{self.contact.pk}"
        data = person_form_data(self.contact, phone="07700 900123")
        data.pop(f"{p}-expected_updated_at")
        response = self.client.post(
            reverse("crm:my_person_edit", args=[self.contact.pk]), data, **HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.phone, "07700 900123")

    def test_non_htmx_get_redirects_to_page(self):
        response = self.client.get(reverse("crm:my_company_edit", args=[self.org.pk]))
        self.assertRedirects(response, reverse("dashboard"), target_status_code=302)

    def test_non_htmx_post_saves_and_redirects(self):
        response = self.client.post(
            reverse("crm:my_company_edit", args=[self.org.pk]),
            company_form_data(self.org, address_line1="99 Fallback Way"),
        )
        self.assertRedirects(response, reverse("dashboard"), target_status_code=302)
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

    def test_detail_shows_quote_and_documents(self):
        quote = make_quote(self.deal)
        self.deal.selected_quote = quote
        self.deal.save(update_fields=["selected_quote"])
        doc = make_document(self.deal, name="Bank statements")

        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, f"{quote.term} months")
        self.assertContains(response, "Bank statements")
        self.assertContains(response, "Requested")
        # Since slice 2, requested documents carry an inline upload form.
        self.assertContains(response, reverse("crm:document_upload", args=[doc.pk]))


from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from crm.models import Document, SignatureRequest

from .factories import make_signature_request

DOCUSEAL_ON = {"DOCUSEAL_URL": "http://sign.test:3000", "DOCUSEAL_API_TOKEN": "tok"}


def _pdf(name="f.pdf"):
    return SimpleUploadedFile(name, b"pdf", content_type="application/pdf")


class DealDocumentUploadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        cls.other_customer, cls.other_deal = make_customer_with_deal()

    def setUp(self):
        self.doc = make_document(self.deal, name="Bank statements")
        self.client.force_login(self.customer)

    def _upload(self, **kwargs):
        return self.client.post(
            reverse("crm:document_upload", args=[self.doc.pk]),
            {"file": _pdf()},
            **kwargs,
        )

    def test_htmx_upload_returns_row_partial_and_logs_note(self):
        response = self._upload(**HTMX)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<html", content)
        self.assertIn("Provided", content)
        self.assertIn("replace", content)

        self.doc.refresh_from_db()
        self.assertTrue(self.doc.is_provided)

        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.deal, self.deal)
        self.assertEqual(note.author, self.customer)
        self.assertIn("Bank statements", note.content)

    def test_htmx_multi_file_upload_notes_file_count(self):
        response = self.client.post(
            reverse("crm:document_upload", args=[self.doc.pk]),
            {"file": [_pdf(), _pdf(), _pdf()]},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Provided", response.content.decode())
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertIn("(3 files)", note.content)

    def test_htmx_upload_without_file_returns_error_row_no_note(self):
        response = self.client.post(
            reverse("crm:document_upload", args=[self.doc.pk]), {}, **HTMX
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Please choose a file", content)
        self.assertIn("Requested", content)
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_provided)
        self.assertEqual(Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).count(), 0)

    def test_foreign_customer_upload_forbidden(self):
        self.client.force_login(self.other_customer)
        response = self._upload(**HTMX)
        self.assertEqual(response.status_code, 403)
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_provided)

    def test_non_htmx_upload_redirects_to_deal_page(self):
        """Non-HTMX customer uploads land back on the /my/ deal page."""
        response = self._upload()
        self.assertRedirects(
            response,
            reverse("crm:my_deal_detail", args=[self.deal.pk]),
            fetch_redirect_response=False,
        )
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.is_provided)

    def test_deal_page_hides_documents_card_when_none_requested(self):
        self.doc.delete()
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertNotContains(response, "Documents")

    def test_deal_page_shows_upload_form_for_requested_doc(self):
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "hx-encoding=\"multipart/form-data\"")
        self.assertContains(response, "Bank statements")


class DealSigningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        cls.other_customer, cls.other_deal = make_customer_with_deal()

    def setUp(self):
        self.client.force_login(self.customer)

    def _sr(self, **extra):
        extra.setdefault("signing_slug", "slug123")
        return make_signature_request(self.deal, **extra)

    def _sign_url(self, sr):
        return reverse("crm:my_deal_sign", args=[self.deal.pk, sr.pk])

    @override_settings(**DOCUSEAL_ON)
    def test_sign_redirects_to_docuseal_signing_page(self):
        # A redirect, not an embed — DocuSeal's embedded form is Pro-only.
        sr = self._sr()
        response = self.client.get(self._sign_url(sr))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "http://sign.test:3000/s/slug123")

    @override_settings(**DOCUSEAL_ON)
    def test_sign_backfills_missing_slug(self):
        sr = self._sr(signing_slug="", submitter_id=42)
        with mock.patch(
            "crm.views.customer.docuseal.get_submission",
            return_value={"submitters": [{"id": 41, "slug": "wrong"}, {"id": 42, "slug": "right42"}]},
        ):
            response = self.client.get(self._sign_url(sr))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "http://sign.test:3000/s/right42")
        sr.refresh_from_db()
        self.assertEqual(sr.signing_slug, "right42")

    @override_settings(**DOCUSEAL_ON)
    def test_sign_page_foreign_deal_404(self):
        sr = make_signature_request(self.other_deal, signing_slug="slugx")
        response = self.client.get(
            reverse("crm:my_deal_sign", args=[self.other_deal.pk, sr.pk])
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(**DOCUSEAL_ON)
    def test_sign_page_inactive_request_redirects(self):
        sr = self._sr(status=SignatureRequest.Status.COMPLETED)
        response = self.client.get(self._sign_url(sr))
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))

    @override_settings(DOCUSEAL_URL="", DOCUSEAL_API_TOKEN="")
    def test_unconfigured_redirects_and_hides_button(self):
        sr = self._sr()
        response = self.client.get(self._sign_url(sr))
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))
        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertNotContains(page, "Sign now")

    @override_settings(**DOCUSEAL_ON)
    def test_deal_page_shows_sign_button_and_signed_link(self):
        active = self._sr()
        signed = self._sr(status=SignatureRequest.Status.COMPLETED)
        signed.signed_file.save("signed.pdf", _pdf("signed.pdf"), save=True)
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, self._sign_url(active))
        self.assertContains(response, reverse("crm:signature_signed_download", args=[signed.pk]))

    def test_signed_download_permissions(self):
        sr = self._sr(status=SignatureRequest.Status.COMPLETED)
        sr.signed_file.save("signed.pdf", _pdf("signed.pdf"), save=True)
        url = reverse("crm:signature_signed_download", args=[sr.pk])

        self.assertEqual(self.client.get(url).status_code, 200)  # own deal
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(url).status_code, 403)  # foreign
        self.client.force_login(self.deal.owner)
        self.assertEqual(self.client.get(url).status_code, 200)  # owning associate
        self.client.force_login(make_associate())
        self.assertEqual(self.client.get(url).status_code, 403)  # other associate

    @override_settings(**DOCUSEAL_ON)
    def test_co_applicant_opens_only_requests_addressed_to_them(self):
        co = make_co_applicant(self.deal, with_user=True, first_name="Chris", last_name="Co")
        theirs = self._sr(signer=co, signer_email=co.email, signer_name="Chris Co", signing_slug="co-slug")
        primarys = self._sr(signer=self.deal.customer, signer_name="Pat Primary")
        self.client.force_login(co.user)

        response = self.client.get(self._sign_url(theirs))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "http://sign.test:3000/s/co-slug")
        self.assertEqual(self.client.get(self._sign_url(primarys)).status_code, 403)

        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(page, self._sign_url(theirs))
        self.assertNotContains(page, self._sign_url(primarys))
        self.assertContains(page, "Awaiting Pat Primary")
        # Signed documents on the deal stay downloadable by every applicant.
        signed = self._sr(status=SignatureRequest.Status.COMPLETED)
        signed.signed_file.save("signed.pdf", _pdf("signed.pdf"), save=True)
        self.assertEqual(
            self.client.get(reverse("crm:signature_signed_download", args=[signed.pk])).status_code, 200,
        )

    @override_settings(**DOCUSEAL_ON)
    def test_primary_cannot_open_a_co_applicants_request(self):
        co = make_co_applicant(self.deal, first_name="Chris", last_name="Co")
        sr = self._sr(signer=co, signer_name="Chris Co")
        self.assertEqual(self.client.get(self._sign_url(sr)).status_code, 403)
        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertNotContains(page, self._sign_url(sr))
        self.assertContains(page, "Awaiting Chris Co")
        self.assertNotContains(page, "Sign now")

    @override_settings(**DOCUSEAL_ON)
    def test_primary_opens_legacy_requests_with_no_signer_recorded(self):
        sr = self._sr()
        self.assertIsNone(sr.signer)
        self.assertEqual(self.client.get(self._sign_url(sr)).status_code, 302)
        co = make_co_applicant(self.deal, with_user=True)
        self.client.force_login(co.user)
        self.assertEqual(self.client.get(self._sign_url(sr)).status_code, 403)


class ActionBadgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        complete_details(cls.deal)

    def setUp(self):
        self.client.force_login(self.customer)

    def test_missing_details_count_and_are_listed(self):
        contact = self.deal.customer
        contact.date_of_birth = None
        contact.save(update_fields=["date_of_birth"])
        org = self.deal.organisation
        org.phone = ""
        org.save(update_fields=["phone"])

        response = self.client.get(reverse("crm:my_deals"))
        self.assertContains(response, "2 actions needed")

        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "What we still need from you")
        self.assertContains(response, "Your details: date of birth")
        self.assertContains(response, f'href="#person-{contact.pk}"')
        self.assertContains(response, "Company details: phone")
        self.assertContains(response, 'href="#company"')
        self.assertContains(response, ">Missing</span>")
        self.assertContains(response, "<span>date of birth</span>")

    def test_nothing_outstanding_shows_success(self):
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "Nothing outstanding")
        self.assertNotContains(response, "What we still need from you")

    def test_badge_counts_outstanding_items(self):
        make_document(self.deal, name="Bank statements")  # requested
        make_signature_request(self.deal)  # sent
        response = self.client.get(reverse("crm:my_deals"))
        self.assertContains(response, "2 actions needed")
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "Upload: Bank statements")
        self.assertContains(response, 'href="#documents"')
        self.assertContains(response, 'href="#signatures"')

    def test_no_badge_when_everything_done(self):
        doc = make_document(self.deal, name="Bank statements")
        doc.attach(_pdf(), by=self.customer)
        make_signature_request(self.deal, status=SignatureRequest.Status.COMPLETED)
        response = self.client.get(reverse("crm:my_deals"))
        self.assertNotContains(response, "needed")

    def test_counts_do_not_multiply(self):
        """Two docs + two signatures = 4, not 2×2 (join-multiplication regression)."""
        make_document(self.deal, name="A")
        make_document(self.deal, name="B")
        make_signature_request(self.deal)
        make_signature_request(self.deal)
        response = self.client.get(reverse("crm:my_deals"))
        self.assertContains(response, "4 actions needed")


class QuoteSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        complete_details(cls.deal)
        cls.other_customer, cls.other_deal = make_customer_with_deal()

    def setUp(self):
        self.quote = make_quote(self.deal)
        self.client.force_login(self.customer)

    def _select(self, quote=None, **kwargs):
        return self.client.post(
            reverse("crm:my_deal_quote_select", args=[self.deal.pk]),
            {"quote": (quote or self.quote).pk},
            **kwargs,
        )

    def test_card_hidden_without_quotes(self):
        self.quote.delete()
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertNotContains(response, "Your quote")

    def test_card_prompts_until_selected(self):
        url = reverse("crm:my_deal_detail", args=[self.deal.pk])
        response = self.client.get(url)
        self.assertContains(response, "Action needed — please choose the quote")
        # Selection happens straight from the radio: the form fires on change,
        # there is no separate submit button.
        self.assertContains(response, 'hx-trigger="change"')
        self.assertNotContains(response, "Choose this quote")

        self._select()
        response = self.client.get(url)
        self.assertNotContains(response, "Action needed — please choose the quote")
        self.assertContains(response, "change your choice at any time")

    def test_htmx_select_returns_card_and_logs_note(self):
        response = self._select(**HTMX)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<html", content)
        self.assertIn("Quote selected — thank you", content)

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.selected_quote_id, self.quote.pk)
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.deal, self.deal)
        self.assertIn("selected a quote", note.content)
        self.assertIn(f"{self.quote.term} months", note.content)

    def test_reselecting_same_quote_logs_no_second_note(self):
        self._select(**HTMX)
        self._select(**HTMX)
        self.assertEqual(Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).count(), 1)

    def test_invalid_selection_rejected(self):
        foreign_quote = make_quote(self.other_deal)
        response = self._select(quote=foreign_quote, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Please pick one of the quotes", response.content.decode())
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.selected_quote_id)

    def test_foreign_deal_404(self):
        self.client.force_login(self.other_customer)
        response = self._select(**HTMX)
        self.assertEqual(response.status_code, 404)
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.selected_quote_id)

    def test_non_htmx_select_redirects(self):
        response = self._select()
        self.assertRedirects(response, reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.selected_quote_id, self.quote.pk)

    def test_unselected_quote_counts_in_badge(self):
        self.assertContains(self.client.get(reverse("crm:my_deals")), "1 action needed")
        detail = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(detail, "Choose your quote")
        self.assertContains(detail, 'href="#quotes"')
        self._select()
        self.assertNotContains(self.client.get(reverse("crm:my_deals")), "needed")

    def test_co_applicant_cannot_select_and_sees_the_card_read_only(self):
        co = make_co_applicant(self.deal, with_user=True)
        complete_details(self.deal)
        self.client.force_login(co.user)
        response = self._select(**HTMX)
        self.assertEqual(response.status_code, 403)
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.selected_quote_id)

        page = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(page, "Your lead applicant chooses the quote")
        self.assertNotContains(page, 'hx-trigger="change"')
        self.assertNotContains(page, "Action needed — please choose the quote")
        self.assertNotContains(page, "Choose your quote")
        self.assertContains(page, "disabled")
        self.assertNotContains(self.client.get(reverse("crm:my_deals")), "needed")


class CommittedQuoteCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def setUp(self):
        self.client.force_login(self.customer)

    def _page(self):
        return self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))

    def test_committed_for_column_toggles(self):
        make_quote(self.deal, term=60)
        self.assertNotContains(self._page(), "Committed for")

        make_quote(self.deal, term=300, commitment_months=60)
        response = self._page()
        self.assertContains(response, "Committed for")
        self.assertContains(response, "60 months")

    def test_committed_quote_selectable(self):
        quote = make_quote(self.deal, term=300, commitment_months=60)
        response = self.client.post(
            reverse("crm:my_deal_quote_select", args=[self.deal.pk]),
            {"quote": quote.pk},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.selected_quote_id, quote.pk)


class CommercialQuoteCardTests(TestCase):
    """Commercial deals' portal quote card shows Lender/Term/Fixed for —
    not the Asset Finance pricing columns — and only Approved terms."""

    @classmethod
    def setUpTestData(cls):
        from crm.models import Deal
        from .factories import make_commercial_quote, make_organisation, make_participation

        cls.customer, cls.deal = make_customer_with_deal()
        Deal.objects.filter(pk=cls.deal.pk).update(type="commercial_finance")
        cls.deal.refresh_from_db()
        cls.lender = make_organisation(name="Big Commercial Bank")
        make_participation(cls.deal, amount=100_000)
        cls.quote = make_commercial_quote(
            cls.deal, lender=cls.lender, term=300, commitment_months=60,
            interest_rate="9.50", status="approved",
        )
        # Still being shopped — the customer never sees this one.
        cls.pending = make_commercial_quote(
            cls.deal, lender=make_organisation(name="Slow Bank plc"),
            term=None, status="submitted",
        )

    def setUp(self):
        self.client.force_login(self.customer)

    def test_card_shows_lender_term_fixed_columns(self):
        response = self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))
        self.assertContains(response, "Big Commercial Bank")
        self.assertNotContains(response, "Slow Bank plc")
        self.assertContains(response, "Fixed for")
        self.assertContains(response, "300 months")
        self.assertContains(response, "60 months")
        content = response.content.decode()
        self.assertNotIn("<th>Deposit</th>", content)
        self.assertNotIn("<th>APR</th>", content)
        # Yield-driven maths: the monthly payment shows for customers.
        self.assertIn("<th>Monthly payment</th>", content)
        self.assertIsNotNone(self.quote.monthly_payment)
        self.assertContains(response, "£{:,.2f}".format(self.quote.monthly_payment).replace(",", ","))

    def test_commercial_quote_selectable(self):
        response = self.client.post(
            reverse("crm:my_deal_quote_select", args=[self.deal.pk]),
            {"quote": self.quote.pk},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.selected_quote_id, self.quote.pk)

    def test_unapproved_terms_not_selectable(self):
        response = self.client.post(
            reverse("crm:my_deal_quote_select", args=[self.deal.pk]),
            {"quote": self.pending.pk},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please pick one of the quotes")
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.selected_quote_id)


class ApplicantManagementTests(TestCase):
    """The primary applicant adds and removes people from their company."""

    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        cls.other_customer, cls.other_deal = make_customer_with_deal()

    def setUp(self):
        self.client.force_login(self.customer)

    def _add(self, **overrides):
        data = {"first_name": "Jane", "last_name": "Partner",
                "email": "jane@example.com", "phone": "0117 111222"}
        data.update(overrides)
        return self.client.post(
            reverse("crm:my_deal_applicant_add", args=[self.deal.pk]), data, follow=True,
        )

    def _remove(self, contact):
        return self.client.post(
            reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, contact.pk]), follow=True,
        )

    def _page(self):
        return self.client.get(reverse("crm:my_deal_detail", args=[self.deal.pk]))

    def test_page_offers_the_add_form_to_the_primary(self):
        page = self._page()
        self.assertContains(page, "Add someone applying with you")
        self.assertContains(page, reverse("crm:my_deal_applicant_add", args=[self.deal.pk]))
        self.assertNotContains(page, "To add someone applying with you, contact")

    def test_add_creates_a_fresh_flagged_contact_in_the_org_with_note_and_staff_email(self):
        stranger = make_contact(email="jane@example.com", first_name="Stranger", last_name="Elsewhere")
        response = self._add()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Added Jane Partner")

        new = self.deal.co_applicants.get()
        self.assertNotEqual(new.pk, stranger.pk)  # never matched by email
        self.assertEqual((new.first_name, new.last_name, new.email, new.phone),
                         ("Jane", "Partner", "jane@example.com", "0117 111222"))
        self.assertIn(self.deal.organisation, new.organisations.all())
        self.assertEqual(new.owner, self.deal.owner)
        stranger.refresh_from_db()
        self.assertEqual(stranger.first_name, "Stranger")

        link = DealCoApplicant.objects.get(deal=self.deal, contact=new)
        self.assertTrue(link.added_by_customer)
        self.assertEqual(link.added_by, self.customer)
        self.assertIsNotNone(link.added_at)

        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.deal, self.deal)
        self.assertEqual(note.contact, new)
        self.assertEqual(note.author, self.customer)
        self.assertIn(self.customer.full_name, note.content)
        self.assertIn("added Jane Partner", note.content)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, settings.NOTIFY_EMAILS)
        self.assertIn("Jane Partner", mail.outbox[0].subject)
        self.assertIn(self.deal.get_absolute_url(), mail.outbox[0].body)

        page = self._page()
        self.assertContains(page, "Jane Partner")
        self.assertContains(page, "added by you")
        self.assertContains(page, reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, new.pk]))
        self.assertContains(page, reverse("crm:my_person_edit", args=[new.pk]))  # primary edits them

    def test_add_rejects_an_email_already_on_the_application(self):
        response = self._add(email=self.deal.customer.email.upper())
        self.assertContains(response, "already on this application")
        self.assertFalse(self.deal.co_applicants.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_add_requires_names_and_email(self):
        response = self._add(first_name="", email="")
        self.assertContains(response, "Couldn&#x27;t add that person")
        self.assertFalse(self.deal.co_applicants.exists())

    def test_add_without_an_organisation_points_at_the_representative(self):
        Deal.objects.filter(pk=self.deal.pk).update(organisation=None)
        page = self._page()
        self.assertNotContains(page, reverse("crm:my_deal_applicant_add", args=[self.deal.pk]))
        self.assertContains(page, "To add someone applying with you, contact your Medifinance representative")
        response = self._add()
        self.assertContains(response, "contact your Medifinance representative")
        self.assertFalse(Contact.objects.filter(email="jane@example.com").exists())

    def test_co_applicant_cannot_add_or_remove(self):
        co = make_co_applicant(self.deal, with_user=True, added_by_customer=True)
        self.client.force_login(co.user)
        response = self.client.post(
            reverse("crm:my_deal_applicant_add", args=[self.deal.pk]),
            {"first_name": "X", "last_name": "Y", "email": "x@example.com"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, co.pk]),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(self.deal.co_applicants.all()), [co])

    def test_foreign_customer_gets_404(self):
        self.client.force_login(self.other_customer)
        response = self.client.post(
            reverse("crm:my_deal_applicant_add", args=[self.deal.pk]),
            {"first_name": "X", "last_name": "Y", "email": "x@example.com"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.deal.co_applicants.exists())

    def test_remove_unlinks_a_customer_added_person_but_keeps_the_contact(self):
        co = make_co_applicant(self.deal, added_by_customer=True)
        response = self._remove(co)
        self.assertContains(response, "Removed")
        self.assertFalse(self.deal.co_applicants.exists())
        self.assertTrue(Contact.objects.filter(pk=co.pk).exists())
        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual(note.deal, self.deal)
        self.assertIn("removed", note.content)

    def test_remove_refused_for_staff_added_people(self):
        co = make_co_applicant(self.deal)
        page = self._page()
        self.assertNotContains(page, reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, co.pk]))
        response = self._remove(co)
        self.assertContains(response, "added by Medifinance")
        self.assertEqual(list(self.deal.co_applicants.all()), [co])

    def test_remove_blocked_by_a_signature_request(self):
        co = make_co_applicant(self.deal, added_by_customer=True)
        make_signature_request(self.deal, signer=co)
        page = self._page()
        self.assertNotContains(page, reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, co.pk]))
        self.assertContains(page, "Can't be removed")
        response = self._remove(co)
        self.assertContains(response, "signature request")
        self.assertEqual(list(self.deal.co_applicants.all()), [co])

    def test_remove_blocked_by_an_upload(self):
        co = make_co_applicant(self.deal, with_user=True, added_by_customer=True)
        make_document(self.deal).attach(_pdf(), by=co.user)
        response = self._remove(co)
        self.assertContains(response, "uploaded documents")
        self.assertEqual(list(self.deal.co_applicants.all()), [co])

    def test_remove_someone_not_on_the_deal_404s(self):
        response = self.client.post(
            reverse("crm:my_deal_applicant_remove", args=[self.deal.pk, self.other_deal.customer_id]),
        )
        self.assertEqual(response.status_code, 404)
