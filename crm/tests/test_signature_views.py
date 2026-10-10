"""The staff-facing signature views: access control, the send flow against a
mocked DocuSeal, graceful degradation when unconfigured, and void/resend."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from crm.docuseal import DocuSealError
from crm.models import SignatureRequest
from crm.tests import factories

TEMPLATES = [{"id": 7, "name": "Finance agreement"}]


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureRequestCreateViewTests(TestCase):
    def setUp(self):
        self.staff = factories.make_associate()
        self.deal = factories.make_deal(owner=self.staff)
        self.url = reverse("crm:deal_sign", args=[self.deal.pk])
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
    def test_post_creates_pending_request_without_emailing(self, _lt, m_create):
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
        self.assertIs(kwargs["send_email"], False)
        self.assertNotIn("message", kwargs)  # the note goes with the send, not the create
        sr = SignatureRequest.objects.get()
        self.assertEqual(sr.deal, self.deal)
        self.assertEqual(sr.submission_id, 555)
        self.assertEqual(sr.submitter_id, 99)
        self.assertEqual(sr.template_name, "Finance agreement")
        self.assertEqual(sr.created_by, self.staff)
        self.assertEqual(sr.message, "Please sign this")
        self.assertEqual(sr.status, SignatureRequest.Status.PENDING)
        self.assertIsNone(sr.sent_at)

    @mock.patch("crm.docuseal.create_submission",
                return_value={"submission_id": 555, "submitter_id": 99})
    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_deal_page_offers_send_not_resend_for_a_pending_request(self, _lt, _create):
        self.client.post(self.url, {
            "template": "7", "signer_email": "jane@example.com", "signer_name": "", "message": "",
        })
        sr = SignatureRequest.objects.get()
        r = self.client.get(self.deal.get_absolute_url())
        self.assertContains(r, reverse("crm:signature_send", args=[sr.pk]))
        self.assertNotContains(r, reverse("crm:signature_resend", args=[sr.pk]))
        self.assertContains(r, reverse("crm:signature_void", args=[sr.pk]))
        self.assertContains(r, "lucide-send")
        self.assertContains(r, ">Pending<")

    @mock.patch("crm.docuseal.create_submission",
                return_value={"submission_id": 556, "submitter_id": 100})
    @mock.patch("crm.docuseal.list_templates", return_value=TEMPLATES)
    def test_concurrent_requests_on_a_deal_are_allowed(self, _lt, _create):
        # Deliberate: a deal can have several requests in flight at once
        # (customer + co-applicants, different templates).
        factories.make_signature_request(self.deal)
        r = self.client.post(self.url, {
            "template": "7",
            "signer_email": "jane@example.com",
            "signer_name": "Jane Doe",
            "message": "",
        })
        self.assertRedirects(r, self.deal.get_absolute_url())
        self.assertEqual(SignatureRequest.objects.count(), 2)


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureSendTests(TestCase):
    def setUp(self):
        self.staff = factories.make_associate()
        self.deal = factories.make_deal(owner=self.staff)
        self.pending = factories.make_signature_request(
            self.deal, submission_id=555, submitter_id=99, template_id=7,
            signer_email="jane@example.com", message="Please sign this",
            status=SignatureRequest.Status.PENDING,
        )
        self.url = reverse("crm:signature_send", args=[self.pending.pk])
        self.client.force_login(self.staff)

    @mock.patch("crm.docuseal.send_submitter_email")
    def test_send_emails_submitter_and_marks_sent(self, m_send):
        r = self.client.post(self.url)
        self.assertRedirects(r, self.deal.get_absolute_url())
        m_send.assert_called_once_with(99, message="Please sign this")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, SignatureRequest.Status.SENT)
        self.assertIsNotNone(self.pending.sent_at)
        # Now the deal page shows re-send rather than send.
        page = self.client.get(self.deal.get_absolute_url())
        self.assertContains(page, reverse("crm:signature_resend", args=[self.pending.pk]))
        self.assertNotContains(page, reverse("crm:signature_send", args=[self.pending.pk]))

    @mock.patch("crm.docuseal.send_submitter_email")
    def test_already_sent_request_is_not_resent_by_send(self, m_send):
        self.pending.status = SignatureRequest.Status.SENT
        self.pending.save(update_fields=["status"])
        r = self.client.post(self.url, follow=True)
        m_send.assert_not_called()
        self.assertContains(r, "already been sent")

    @mock.patch("crm.docuseal.send_submitter_email",
                side_effect=DocuSealError("nope"))
    def test_docuseal_error_leaves_pending_request_untouched(self, _m):
        r = self.client.post(self.url, follow=True)
        self.assertContains(r, "Couldn&#x27;t send via DocuSeal")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, SignatureRequest.Status.PENDING)

    @mock.patch("crm.docuseal.send_submitter_email")
    def test_resend_refuses_a_pending_request(self, m_send):
        r = self.client.post(reverse("crm:signature_resend", args=[self.pending.pk]), follow=True)
        self.assertContains(r, "use Send instead")
        m_send.assert_not_called()
        self.assertEqual(SignatureRequest.objects.count(), 1)

    @mock.patch("crm.docuseal.archive_submission")
    def test_pending_request_can_be_voided(self, m_archive):
        self.client.post(reverse("crm:signature_void", args=[self.pending.pk]))
        m_archive.assert_called_once_with(555)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, SignatureRequest.Status.VOIDED)

    def test_customer_portal_offers_pending_request_to_sign(self):
        # Pending means "not emailed yet" — the customer can still sign it
        # from their portal, so it's listed with a Sign now link.
        customer, deal = factories.make_customer_with_deal(owner=self.staff)
        sr = factories.make_signature_request(
            deal, status=SignatureRequest.Status.PENDING, template_name="HP agreement",
            signing_slug="slug123",
        )
        self.client.force_login(customer)
        r = self.client.get(reverse("crm:my_deal_detail", args=[deal.pk]))
        self.assertContains(r, "HP agreement")
        self.assertContains(r, "Awaiting signature")
        self.assertContains(r, reverse("crm:my_deal_sign", args=[deal.pk, sr.pk]))
        self.assertIn("Sign: HP agreement", [i.label for i in r.context["outstanding"]])
        # ... and the sign-now redirect to DocuSeal works for it.
        r = self.client.get(reverse("crm:my_deal_sign", args=[deal.pk, sr.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "http://sign.test/s/slug123")

    def test_customers_cannot_send(self):
        customer, _deal = factories.make_customer_with_deal()
        self.client.force_login(customer)
        self.assertEqual(self.client.post(self.url).status_code, 403)


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureVoidResendTests(TestCase):
    def setUp(self):
        self.staff = factories.make_associate()
        self.deal = factories.make_deal(owner=self.staff)
        self.sr = factories.make_signature_request(
            self.deal, submission_id=555, template_id=7, signer_email="jane@example.com",
            message="Please sign this")
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
        self.assertEqual(fresh.status, SignatureRequest.Status.SENT)  # re-sends go straight out
        self.assertIsNotNone(fresh.sent_at)
        self.assertEqual(fresh.deal, self.deal)
        self.assertEqual(fresh.signer_email, "jane@example.com")
        self.assertEqual(fresh.template_id, 7)
        self.assertEqual(fresh.message, "Please sign this")
        self.assertEqual(m_create.call_args.kwargs["signer_email"], "jane@example.com")
        self.assertEqual(m_create.call_args.kwargs["message"], "Please sign this")

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
