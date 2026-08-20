"""The DocuSeal webhook: secret-header auth, lifecycle transitions, the
verify-then-fetch completion flow and its idempotency."""

import json
import tempfile
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from crm.models import SignatureRequest
from crm.tests import factories

SECRET = "whsec-test"


@override_settings(DOCUSEAL_WEBHOOK_SECRET=SECRET, MEDIA_ROOT=tempfile.mkdtemp())
class DocuSealWebhookTests(TestCase):
    def setUp(self):
        self.deal = factories.make_deal()
        self.sr = factories.make_signature_request(self.deal, submission_id=555)
        self.url = reverse("crm:docuseal_webhook")

    def _post(self, event, data, *, secret=SECRET):
        headers = {"X-Docuseal-Secret": secret} if secret is not None else {}
        return self.client.post(
            self.url,
            data=json.dumps({"event_type": event, "timestamp": "2026-08-10T12:00:00Z", "data": data}),
            content_type="application/json",
            headers=headers,
        )

    # -- auth ---------------------------------------------------------------

    def test_missing_or_wrong_secret_is_401(self):
        self.assertEqual(self._post("form.viewed", {"submission_id": 555}, secret=None).status_code, 401)
        self.assertEqual(self._post("form.viewed", {"submission_id": 555}, secret="nope").status_code, 401)

    @override_settings(DOCUSEAL_WEBHOOK_SECRET="")
    def test_unconfigured_environment_is_503(self):
        self.assertEqual(self._post("form.viewed", {"submission_id": 555}).status_code, 503)

    def test_bad_json_is_400(self):
        r = self.client.post(self.url, data="{not json", content_type="application/json",
                             headers={"X-Docuseal-Secret": SECRET})
        self.assertEqual(r.status_code, 400)

    def test_unknown_submission_is_acked_with_200(self):
        self.assertEqual(self._post("form.completed", {"submission_id": 99999}).status_code, 200)

    # -- lifecycle ----------------------------------------------------------

    def test_viewed_flips_sent_to_opened(self):
        r = self._post("form.viewed", {"submission_id": 555})
        self.assertEqual(r.status_code, 200)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.OPENED)
        self.assertIsNotNone(self.sr.opened_at)

    def test_declined_records_reason(self):
        r = self._post("form.declined", {"submission_id": 555, "decline_reason": "Wrong amount"})
        self.assertEqual(r.status_code, 200)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.DECLINED)
        self.assertEqual(self.sr.decline_reason, "Wrong amount")

    def test_late_viewed_does_not_regress_completed(self):
        self.sr.status = SignatureRequest.Status.COMPLETED
        self.sr.save(update_fields=["status"])
        self._post("form.viewed", {"submission_id": 555})
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.COMPLETED)

    # -- completion ---------------------------------------------------------

    def _mock_completed_api(self, m_get_submission, m_get_docs, m_download):
        m_get_submission.return_value = {
            "status": "completed",
            "audit_log_url": "http://sign.test/audit/555.pdf",
            "submitters": [{"ip": "203.0.113.9", "ua": "Safari"}],
        }
        m_get_docs.return_value = [{"name": "agreement", "url": "http://sign.test/docs/555.pdf"}]
        m_download.side_effect = lambda url: b"%PDF audit" if "audit" in url else b"%PDF signed"

    @mock.patch("crm.docuseal.download_file")
    @mock.patch("crm.docuseal.get_submission_documents")
    @mock.patch("crm.docuseal.get_submission")
    def test_completed_attaches_signed_pdf_and_audit_log(self, m_sub, m_docs, m_dl):
        self._mock_completed_api(m_sub, m_docs, m_dl)
        r = self._post("form.completed", {"submission_id": 555})
        self.assertEqual(r.status_code, 200)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.COMPLETED)
        self.assertIn("test-agreement-signed", self.sr.signed_file.name)
        self.assertTrue(self.sr.audit_log_file)
        self.assertEqual(self.sr.signer_ip, "203.0.113.9")
        self.assertEqual(self.sr.signer_user_agent, "Safari")
        self.assertIsNotNone(self.sr.completed_at)

    @mock.patch("crm.docuseal.download_file")
    @mock.patch("crm.docuseal.get_submission_documents")
    @mock.patch("crm.docuseal.get_submission")
    def test_duplicate_completed_delivery_is_idempotent(self, m_sub, m_docs, m_dl):
        self._mock_completed_api(m_sub, m_docs, m_dl)
        self._post("form.completed", {"submission_id": 555})
        first_file = SignatureRequest.objects.get(pk=self.sr.pk).signed_file.name
        r = self._post("form.completed", {"submission_id": 555})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SignatureRequest.objects.get(pk=self.sr.pk).signed_file.name, first_file)
        # The API was only consulted for the first delivery.
        self.assertEqual(m_sub.call_count, 1)

    @mock.patch("crm.docuseal.get_submission")
    def test_completed_event_not_confirmed_by_api_is_ignored(self, m_sub):
        m_sub.return_value = {"status": "pending"}
        r = self._post("form.completed", {"submission_id": 555})
        self.assertEqual(r.status_code, 200)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.SENT)

    @mock.patch("crm.docuseal.get_submission")
    def test_docuseal_unreachable_asks_for_retry(self, m_sub):
        from crm.docuseal import DocuSealError
        m_sub.side_effect = DocuSealError("boom")
        r = self._post("form.completed", {"submission_id": 555})
        self.assertEqual(r.status_code, 502)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.SENT)
