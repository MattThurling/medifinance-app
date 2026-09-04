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

    def test_wizard_upload_redirect_unchanged(self):
        """Non-HTMX customer uploads keep the wizard redirect — regression."""
        response = self._upload()
        self.assertRedirects(
            response,
            reverse("crm:portal_documents", args=[self.deal.pk]),
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
            "crm.views_customer.docuseal.get_submission",
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
        self.client.force_login(make_associate())
        self.assertEqual(self.client.get(url).status_code, 200)  # staff


class ActionBadgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def setUp(self):
        self.client.force_login(self.customer)

    def test_badge_counts_outstanding_items(self):
        make_document(self.deal, name="Bank statements")  # requested
        make_signature_request(self.deal)  # sent
        for url in [reverse("crm:my_deals"), reverse("dashboard")]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "2 actions needed")

    def test_no_badge_when_everything_done(self):
        doc = make_document(self.deal, name="Bank statements")
        doc.attach(_pdf(), by=self.customer)
        make_signature_request(self.deal, status=SignatureRequest.Status.COMPLETED)
        for url in [reverse("crm:my_deals"), reverse("dashboard")]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotContains(response, "needed")

    def test_counts_do_not_multiply(self):
        """Two docs + two signatures = 4, not 2×2 (distinct=True regression)."""
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
        for url in [reverse("crm:my_deals"), reverse("dashboard")]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "1 action needed")
        self._select()
        for url in [reverse("crm:my_deals"), reverse("dashboard")]:
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "needed")
