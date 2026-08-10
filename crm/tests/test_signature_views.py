"""The staff-facing signature views: access control, the send flow against a
mocked DocuSeal, graceful degradation when unconfigured, and void/resend."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from crm.models import SignatureRequest
from crm.tests import factories

TEMPLATES = [{"id": 7, "name": "Finance agreement"}]


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureRequestCreateViewTests(TestCase):
    def setUp(self):
        self.staff = factories.make_associate()
        self.deal = factories.make_deal()
        self.doc = factories.make_document(self.deal, name="Finance agreement")
        self.url = reverse("crm:document_sign", args=[self.doc.pk])
        self.client.force_login(self.staff)

    def test_customers_cannot_send_for_signature(self):
        customer, _deal = factories.make_customer_with_deal()
        self.client.force_login(customer)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    @override_settings(DOCUSEAL_URL="")
    def test_unconfigured_environment_redirects_with_message(self):
        r = self.client.get(self.url, follow=True)
        self.assertRedirects(r, self.deal.get_absolute_url())
        self.assertIn("isn't configured", str(list(r.context["messages"])[0]))

    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_get_prefills_customer_as_signer(self, _lt):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertEqual(form.initial["signer_email"], self.deal.customer.email)
        self.assertEqual(form.initial["signer_name"], self.deal.customer.full_name)
        self.assertEqual(form.fields["template"].choices, [(7, "Finance agreement")])

    @mock.patch("crm.docuseal.create_submission",
                return_value={"submission_id": 555, "submitter_id": 99})
    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_post_creates_submission_with_prefilled_values(self, _lt, m_create):
        r = self.client.post(self.url, {
            "template": "7",
            "signer": self.deal.customer.pk,
            "signer_email": "jane@example.com",
            "signer_name": "Jane Doe",
            "message": "Please sign this",
        })
        self.assertRedirects(r, self.deal.get_absolute_url())
        kwargs = m_create.call_args.kwargs
        self.assertEqual(kwargs["template_id"], 7)
        self.assertEqual(kwargs["signer_email"], "jane@example.com")
        self.assertEqual(kwargs["values"]["Deal Name"], self.deal.name)
        self.assertEqual(kwargs["message"], "Please sign this")
        sr = SignatureRequest.objects.get()
        self.assertEqual(sr.document, self.doc)
        self.assertEqual(sr.submission_id, 555)
        self.assertEqual(sr.template_name, "Finance agreement")
        self.assertEqual(sr.created_by, self.staff)
        self.assertEqual(sr.status, SignatureRequest.Status.SENT)

    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_active_request_blocks_a_second_send(self, _lt):
        factories.make_signature_request(self.doc)
        r = self.client.get(self.url)
        self.assertRedirects(r, self.deal.get_absolute_url())

    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_provided_document_cannot_be_sent(self, _lt):
        from django.core.files.base import ContentFile
        import tempfile
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            self.doc.attach(ContentFile(b"x", name="x.pdf"))
        r = self.client.get(self.url)
        self.assertRedirects(r, self.deal.get_absolute_url())


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureVoidResendTests(TestCase):
    def setUp(self):
        self.staff = factories.make_associate()
        self.deal = factories.make_deal()
        self.doc = factories.make_document(self.deal)
        self.sr = factories.make_signature_request(
            self.doc, submission_id=555, template_id=7, signer_email="jane@example.com")
        self.client.force_login(self.staff)

    @mock.patch("crm.docuseal.archive_submission")
    def test_void_archives_and_marks_voided(self, m_archive):
        r = self.client.post(reverse("crm:signature_void", args=[self.sr.pk]))
        self.assertRedirects(r, self.deal.get_absolute_url())
        m_archive.assert_called_once_with(555)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.VOIDED)

    def test_completed_request_cannot_be_voided(self):
        self.sr.status = SignatureRequest.Status.COMPLETED
        self.sr.save(update_fields=["status"])
        self.client.post(reverse("crm:signature_void", args=[self.sr.pk]))
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.COMPLETED)

    @mock.patch("crm.docuseal.create_submission",
                return_value={"submission_id": 556, "submitter_id": 100})
    @mock.patch("crm.docuseal.archive_submission")
    def test_resend_voids_old_and_creates_new(self, m_archive, m_create):
        r = self.client.post(reverse("crm:signature_resend", args=[self.sr.pk]))
        self.assertRedirects(r, self.deal.get_absolute_url())
        m_archive.assert_called_once_with(555)
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.VOIDED)
        fresh = SignatureRequest.objects.get(submission_id=556)
        self.assertEqual(fresh.status, SignatureRequest.Status.SENT)
        self.assertEqual(fresh.signer_email, "jane@example.com")
        self.assertEqual(fresh.template_id, 7)
        self.assertEqual(m_create.call_args.kwargs["signer_email"], "jane@example.com")

    @mock.patch("crm.docuseal.create_submission",
                return_value={"submission_id": 556, "submitter_id": 100})
    @mock.patch("crm.docuseal.archive_submission")
    def test_resend_after_decline_keeps_declined_history(self, m_archive, m_create):
        self.sr.status = SignatureRequest.Status.DECLINED
        self.sr.save(update_fields=["status"])
        self.client.post(reverse("crm:signature_resend", args=[self.sr.pk]))
        m_archive.assert_not_called()  # nothing live to archive
        self.sr.refresh_from_db()
        self.assertEqual(self.sr.status, SignatureRequest.Status.DECLINED)
        self.assertTrue(SignatureRequest.objects.filter(submission_id=556).exists())

    def test_customers_cannot_manage_signatures(self):
        customer, _deal = factories.make_customer_with_deal()
        self.client.force_login(customer)
        self.assertEqual(
            self.client.post(reverse("crm:signature_void", args=[self.sr.pk])).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("crm:signature_resend", args=[self.sr.pk])).status_code, 403)
