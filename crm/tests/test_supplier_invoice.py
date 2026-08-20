"""Token-gated supplier invoice upload — no login, the token IS the auth.

Same UX shape as the customer MagicLink: single-use, time-limited. The view
is public, so the failure modes (unknown / consumed / expired token) are the
entire security surface."""

import tempfile
from datetime import timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import ParticipationInvoiceLink, Stage

from .factories import (
    make_associate,
    make_contact,
    make_deal,
    make_organisation,
    make_participation,
    make_proposal,
)


class OrganisationFormalNameTests(TestCase):
    """formal_name is the client name used on formal correspondence
    (e.g. the supplier invoice request email)."""

    def test_legal_name_alone(self):
        org = make_organisation(name="Everyday", legal_name="Legal Ltd")
        self.assertEqual(org.formal_name, "Legal Ltd")

    def test_legal_plus_trading(self):
        org = make_organisation(
            name="Everyday", legal_name="Legal Ltd", trading_name="Smile Dental",
        )
        self.assertEqual(org.formal_name, "Legal Ltd trading as Smile Dental")

    def test_identical_legal_and_trading_not_repeated(self):
        org = make_organisation(
            name="Everyday", legal_name="Legal Ltd", trading_name="Legal Ltd",
        )
        self.assertEqual(org.formal_name, "Legal Ltd")

    def test_falls_back_to_name(self):
        org = make_organisation(name="Everyday", trading_name="Smile Dental")
        self.assertEqual(org.formal_name, "Everyday")


class RequestSupplierInvoiceEmailTests(TestCase):
    """Staff 'request invoice' action — the email must name the client by its
    formal (legal/trading) name and carry the shared Reply-To address."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        client_org = make_organisation(
            name="Client Co", legal_name="Client Co Ltd", trading_name="Smiles",
        )
        cls.deal = make_deal(owner=cls.staff, organisation=client_org)
        cls.deal.selected_proposal = make_proposal(cls.deal)
        cls.deal.save(update_fields=["selected_proposal"])
        supplier = make_organisation(name="Acme Supplies")
        cls.participation = make_participation(
            cls.deal, organisation=supplier, amount="5000",
        )
        cls.participation.invoice_contact = make_contact(
            organisation=supplier, email="billing@acme.example",
        )
        cls.participation.save(update_fields=["invoice_contact"])

    def test_email_uses_formal_client_name_and_reply_to(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("crm:participation_request_invoice", args=[self.participation.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["billing@acme.example"])
        self.assertIn("Client Co Ltd trading as Smiles", message.subject)
        self.assertIn("Client Co Ltd trading as Smiles", message.body)
        self.assertEqual(message.reply_to, ["info@medifinance.co.uk"])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), NOTIFY_EMAILS=["staff@medi-finance.co.uk"])
class SupplierInvoiceSubmitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.supplier = make_organisation(name="Acme Supplies")
        cls.deal = make_deal(owner=cls.staff, organisation=make_organisation(name="Client Co"))
        cls.participation = make_participation(
            cls.deal, organisation=cls.supplier, amount="5000",
        )

    def _issue_link(self):
        return ParticipationInvoiceLink.issue(
            participation=self.participation, created_by=self.staff,
        )

    def _url(self, token):
        return reverse("crm:participation_submit_invoice", args=[token])

    def test_unknown_token_returns_404(self):
        response = self.client.get(self._url("nothing-matches-this"))
        self.assertEqual(response.status_code, 404)

    def test_valid_token_renders_upload_form(self):
        link = self._issue_link()
        response = self.client.get(self._url(link.token))
        self.assertEqual(response.status_code, 200)
        # The form names the *client* (the deal's organisation) so the
        # supplier knows whose invoice they're submitting.
        self.assertContains(response, "Client Co")

    def test_consumed_token_returns_410(self):
        link = self._issue_link()
        link.consume()
        response = self.client.get(self._url(link.token))
        self.assertEqual(response.status_code, 410)

    def test_expired_token_returns_410(self):
        link = self._issue_link()
        link.expires_at = timezone.now() - timedelta(seconds=1)
        link.save(update_fields=["expires_at"])
        response = self.client.get(self._url(link.token))
        self.assertEqual(response.status_code, 410)

    def test_valid_post_attaches_invoice_consumes_link_and_records_stage(self):
        link = self._issue_link()
        stage_count_before = Stage.objects.filter(deal=self.deal).count()
        response = self.client.post(
            self._url(link.token),
            {
                "invoice_number": "INV-001",
                "invoice": SimpleUploadedFile("inv.pdf", b"%PDF-INV", content_type="application/pdf"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.participation.refresh_from_db()
        self.assertTrue(self.participation.invoice)
        self.assertEqual(self.participation.invoice_number, "INV-001")
        link.refresh_from_db()
        self.assertTrue(link.is_consumed)
        # An "Invoice Received" Stage was logged on the deal.
        self.assertEqual(
            Stage.objects.filter(deal=self.deal).count(), stage_count_before + 1,
        )
        latest = Stage.objects.filter(deal=self.deal).order_by("-occurred_at", "-pk").first()
        self.assertEqual(latest.name, Stage.Name.SUPPLIER_INVOICE_RECEIVED)

    def test_valid_post_notifies_staff(self):
        link = self._issue_link()
        self.client.post(
            self._url(link.token),
            {
                "invoice_number": "INV-007",
                "invoice": SimpleUploadedFile("inv.pdf", b"%PDF-INV", content_type="application/pdf"),
            },
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["staff@medi-finance.co.uk"])
        self.assertIn("Client Co", message.subject)
        self.assertIn("Acme Supplies", message.body)
        self.assertIn("INV-007", message.body)
        self.assertIn(self.deal.get_absolute_url(), message.body)

    def test_missing_invoice_number_is_rejected(self):
        # The deal page links to the PDF via the invoice number — without one
        # there is nothing to click, so the upload form requires it.
        link = self._issue_link()
        response = self.client.post(
            self._url(link.token),
            {"invoice": SimpleUploadedFile("inv.pdf", b"%PDF-INV", content_type="application/pdf")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.participation.refresh_from_db()
        self.assertFalse(self.participation.invoice)
        link.refresh_from_db()
        self.assertFalse(link.is_consumed)

    def test_consumed_token_cannot_be_reused_for_post(self):
        link = self._issue_link()
        link.consume()
        response = self.client.post(
            self._url(link.token),
            {
                "invoice_number": "FORGED",
                "invoice": SimpleUploadedFile("x.pdf", b"%PDF", content_type="application/pdf"),
            },
        )
        self.assertEqual(response.status_code, 410)
        self.participation.refresh_from_db()
        self.assertFalse(self.participation.invoice)
        self.assertNotEqual(self.participation.invoice_number, "FORGED")
