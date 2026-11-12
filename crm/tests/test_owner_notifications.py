"""Deal-owner notification emails (`crm.notifications`): the owner is told
when a quote is selected, a document is uploaded, an e-signature is completed
and a supplier uploads their invoice. They are not told about their own
actions, and an unowned deal falls back to the NOTIFY_EMAILS staff list."""

import json
import tempfile
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.models import Deal, SignatureRequest

from .factories import (
    make_admin,
    make_associate,
    make_customer_with_deal,
    make_document,
    make_participation,
    make_quote,
    make_signature_request,
)

STAFF_LIST = ["staff-list@medifinance.test"]
SECRET = "whsec-test"


def _pdf(name="ok.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4", content_type="application/pdf")


@override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(), NOTIFY_EMAILS=STAFF_LIST, DOCUSEAL_WEBHOOK_SECRET=SECRET,
)
class OwnerNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_associate()
        cls.admin = make_admin()
        cls.customer, cls.deal = make_customer_with_deal(owner=cls.owner)
        make_participation(cls.deal, amount="10000.00")
        cls.quote = make_quote(cls.deal, term=60)
        cls.document = make_document(cls.deal, name="Bank statements")

    def _only_email(self):
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def _disown(self):
        Deal.objects.filter(pk=self.deal.pk).update(owner=None)

    # -- quote selected -------------------------------------------------------

    def _customer_selects(self):
        self.client.force_login(self.customer)
        return self.client.post(
            reverse("crm:my_deal_quote_select", args=[self.deal.pk]), {"quote": self.quote.pk},
        )

    def test_customer_selecting_a_quote_emails_the_owner(self):
        self._customer_selects()
        message = self._only_email()
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn("Quote selected", message.subject)
        self.assertIn(self.deal.name, message.subject)
        self.assertIn("60 months", message.body)
        self.assertIn(self.customer.full_name, message.body)
        self.assertIn(self.deal.get_absolute_url(), message.body)

    def test_reselecting_the_same_quote_sends_nothing_more(self):
        self._customer_selects()
        self._customer_selects()
        self.assertEqual(len(mail.outbox), 1)

    def test_colleague_selecting_a_quote_emails_the_owner(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("crm:quote_select", args=[self.quote.pk]))
        message = self._only_email()
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn(self.admin.full_name, message.body)

    def test_owner_selecting_a_quote_is_not_emailed(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("crm:quote_select", args=[self.quote.pk]))
        self.assertEqual(mail.outbox, [])

    def test_unowned_deal_falls_back_to_the_staff_list(self):
        self._disown()
        self._customer_selects()
        self.assertEqual(self._only_email().to, STAFF_LIST)

    def test_inactive_owner_falls_back_to_the_staff_list(self):
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        self._customer_selects()
        self.assertEqual(self._only_email().to, STAFF_LIST)

    def test_mail_failure_does_not_break_the_selection(self):
        with mock.patch(
            "accounts.emails.send_deal_owner_notification_email", side_effect=OSError("smtp down"),
        ), self.assertLogs("crm.notifications", level="ERROR"):
            response = self._customer_selects()
        self.assertLess(response.status_code, 400)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.selected_quote, self.quote)

    # -- document uploaded ----------------------------------------------------

    def _upload(self, user, files=None):
        self.client.force_login(user)
        return self.client.post(
            reverse("crm:document_upload", args=[self.document.pk]),
            {"file": files or _pdf()},
        )

    def test_customer_upload_emails_the_owner(self):
        self._upload(self.customer)
        message = self._only_email()
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn("Document uploaded: Bank statements", message.subject)
        self.assertIn(self.customer.full_name, message.body)
        self.assertNotIn("Files:", message.body)

    def test_multi_file_upload_reports_the_count(self):
        self._upload(self.customer, files=[_pdf("a.pdf"), _pdf("b.pdf")])
        self.assertIn("Files: 2", self._only_email().body)

    def test_owner_upload_is_not_emailed(self):
        self._upload(self.owner)
        self.assertEqual(mail.outbox, [])

    def test_rejected_upload_sends_nothing(self):
        self.client.force_login(self.customer)
        self.client.post(reverse("crm:document_upload", args=[self.document.pk]), {})
        self.assertEqual(mail.outbox, [])

    # -- e-signature completed ------------------------------------------------

    def _complete(self, sr):
        with mock.patch("crm.docuseal.get_submission") as m_sub, \
                mock.patch("crm.docuseal.get_submission_documents") as m_docs, \
                mock.patch("crm.docuseal.download_file", return_value=b"%PDF-1.4"):
            m_sub.return_value = {"status": "completed", "submitters": [{}]}
            m_docs.return_value = [{"url": "https://docuseal.example/signed.pdf"}]
            return self.client.post(
                reverse("crm:docuseal_webhook"),
                data=json.dumps({"event_type": "form.completed",
                                 "data": {"submission_id": sr.submission_id}}),
                content_type="application/json",
                headers={"X-Docuseal-Secret": SECRET},
            )

    def test_completed_signature_emails_the_owner_once(self):
        sr = make_signature_request(self.deal, signer_name="Sam Signer")
        self.assertEqual(self._complete(sr).status_code, 200)
        self.assertEqual(self._complete(sr).status_code, 200)  # duplicate delivery

        message = self._only_email()
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn("Signed: Test agreement", message.subject)
        self.assertIn("Sam Signer", message.body)
        sr.refresh_from_db()
        self.assertEqual(sr.status, SignatureRequest.Status.COMPLETED)

    def test_viewed_event_sends_nothing(self):
        sr = make_signature_request(self.deal)
        self.client.post(
            reverse("crm:docuseal_webhook"),
            data=json.dumps({"event_type": "form.viewed",
                             "data": {"submission_id": sr.submission_id}}),
            content_type="application/json",
            headers={"X-Docuseal-Secret": SECRET},
        )
        self.assertEqual(mail.outbox, [])

    def test_mail_failure_still_acknowledges_the_webhook(self):
        sr = make_signature_request(self.deal)
        with mock.patch(
            "accounts.emails.send_deal_owner_notification_email", side_effect=OSError("smtp down"),
        ), self.assertLogs("crm.notifications", level="ERROR"):
            response = self._complete(sr)
        self.assertEqual(response.status_code, 200)
        sr.refresh_from_db()
        self.assertEqual(sr.status, SignatureRequest.Status.COMPLETED)
