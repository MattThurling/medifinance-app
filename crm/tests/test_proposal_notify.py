"""Notify-client-of-approved-proposal — the send is gated on the proposal
being Approved AND selected, the customer having an email, and it not having
been sent before (`Proposal.notified_at` blocks a repeat)."""

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from crm.models import Proposal, Stage

from .factories import (
    make_associate,
    make_deal,
    make_participation,
    make_proposal,
    make_quote,
)


class ProposalNotifyClientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)
        cls.customer = cls.deal.customer
        # A participation gives the deal a funded amount, so the selected
        # quote can compute finance_amount and monthly_payment.
        make_participation(cls.deal, amount="10000.00")
        cls.quote = make_quote(cls.deal, term=60)
        cls.proposal = make_proposal(
            cls.deal, status=Proposal.Status.APPROVED, proposal_number="LND-123",
        )
        cls.deal.selected_proposal = cls.proposal
        cls.deal.selected_quote = cls.quote
        cls.deal.save(update_fields=["selected_proposal", "selected_quote"])

    def setUp(self):
        self.client.force_login(self.staff)

    def _url(self, proposal=None):
        return reverse("crm:proposal_notify", args=[(proposal or self.proposal).pk])

    def _post(self, proposal=None):
        return self.client.post(self._url(proposal))

    def test_notify_sends_email_with_details_and_records_send(self):
        response = self._post()
        self.assertRedirects(response, self.deal.get_absolute_url())

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.customer.email])
        self.assertIn(self.proposal.lender.name, message.subject)
        self.assertIn("LND-123", message.body)
        self.assertIn("60 months", message.body)
        self.assertIn("Monthly payment", message.body)

        self.proposal.refresh_from_db()
        self.assertIsNotNone(self.proposal.notified_at)
        latest = Stage.objects.filter(deal=self.deal).order_by("-occurred_at", "-pk").first()
        self.assertEqual(latest.name, Stage.Name.CLIENT_NOTIFIED)
        self.assertEqual(latest.organisation, self.proposal.lender)
        self.assertEqual(latest.set_by, self.staff)

    def test_notify_without_selected_quote_omits_quote_details(self):
        self.deal.selected_quote = None
        self.deal.save(update_fields=["selected_quote"])

        self._post()

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertNotIn("Term", body)
        self.assertNotIn("Monthly payment", body)
        self.assertIn("LND-123", body)

    def test_notify_rejected_when_proposal_not_approved(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(
            status=Proposal.Status.SUBMITTED,
        )
        self._assert_nothing_sent()

    def test_notify_rejected_when_proposal_not_selected(self):
        self.deal.selected_proposal = None
        self.deal.save(update_fields=["selected_proposal"])
        self._assert_nothing_sent()

    def test_notify_rejected_when_customer_has_no_email(self):
        self.customer.email = ""
        self.customer.save(update_fields=["email"])
        self._assert_nothing_sent()

    def test_notify_cannot_be_repeated(self):
        self._post()
        self.assertEqual(len(mail.outbox), 1)
        stage_count = Stage.objects.filter(deal=self.deal).count()

        mail.outbox.clear()
        response = self._post()
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Stage.objects.filter(deal=self.deal).count(), stage_count)

    def _assert_nothing_sent(self):
        response = self._post()
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 0)
        self.proposal.refresh_from_db()
        self.assertIsNone(self.proposal.notified_at)
        self.assertFalse(
            Stage.objects.filter(deal=self.deal, name=Stage.Name.CLIENT_NOTIFIED).exists()
        )
