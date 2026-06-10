"""Token-gated supplier invoice upload — no login, the token IS the auth.

Same UX shape as the customer MagicLink: single-use, time-limited. The view
is public, so the failure modes (unknown / consumed / expired token) are the
entire security surface."""

import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import ParticipationInvoiceLink, Stage

from .factories import (
    make_associate,
    make_deal,
    make_organisation,
    make_participation,
)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
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
        self.assertEqual(latest.name, Stage.Name.INVOICE_RECEIVED)

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
