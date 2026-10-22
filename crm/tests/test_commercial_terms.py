"""Terms rows on commercial deals: one lender's status plus the terms they
came back with, folded into Quote. The Proposal-style bookkeeping (stage
events, client notify, commission invoice addressing) must behave exactly
as it does for Proposals on Asset Finance deals, and Asset Finance deals
must be untouched."""

from datetime import date
from decimal import Decimal

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from crm.forms import TermsForm
from crm.models import Deal, LenderStatus, Quote, Stage

from .factories import (
    make_associate,
    make_commercial_quote,
    make_deal,
    make_organisation,
    make_participation,
    make_quote,
    make_xero_connection,
)


def _commercial_deal(owner, deal_type=Deal.Type.BUSINESS_LOANS, amount=None, **extra):
    deal = make_deal(owner=owner, **extra)
    Deal.objects.filter(pk=deal.pk).update(type=deal_type)
    deal.refresh_from_db()
    if amount:
        make_participation(deal, amount=amount)
    return deal


class TermsStageEmissionTests(TestCase):
    """Creating a Terms row, or changing its status, appends the same stage
    event a Proposal would — with the lender attached. Asset Finance quotes
    never emit stages."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = _commercial_deal(cls.staff)
        cls.lender = make_organisation(name="Terms Bank")

    def setUp(self):
        self.client.force_login(self.staff)

    def _latest_stage(self, deal):
        return Stage.objects.filter(deal=deal).order_by("-occurred_at", "-pk").first()

    def test_create_emits_submitted_stage_with_lender(self):
        response = self.client.post(
            reverse("crm:quote_create") + f"?deal={self.deal.pk}",
            {"lender": str(self.lender.pk), "status": "submitted"},
        )
        self.assertRedirects(response, self.deal.get_absolute_url())
        stage = self._latest_stage(self.deal)
        self.assertEqual(stage.name, Stage.Name.PROPOSAL_SUBMITTED)
        self.assertEqual(stage.organisation, self.lender)
        self.assertEqual(stage.set_by, self.staff)

    def test_status_change_emits_matching_stage(self):
        quote = make_commercial_quote(self.deal, lender=self.lender, term=None)
        before = Stage.objects.filter(deal=self.deal).count()
        response = self.client.post(
            reverse("crm:quote_update", args=[quote.pk]),
            {"lender": str(self.lender.pk), "status": "approved",
             "interest_rate_percent": "8.25", "term": "120"},
        )
        self.assertRedirects(response, self.deal.get_absolute_url())
        quote.refresh_from_db()
        self.assertEqual((quote.status, quote.term), ("approved", 120))
        self.assertEqual(Stage.objects.filter(deal=self.deal).count(), before + 1)
        self.assertEqual(self._latest_stage(self.deal).name, Stage.Name.PROPOSAL_APPROVED)

    def test_edit_without_status_change_emits_nothing(self):
        quote = make_commercial_quote(
            self.deal, lender=self.lender, term=120, interest_rate="8.25", status="approved",
        )
        before = Stage.objects.filter(deal=self.deal).count()
        self.client.post(
            reverse("crm:quote_update", args=[quote.pk]),
            {"lender": str(self.lender.pk), "status": "approved",
             "interest_rate_percent": "8.00", "term": "120"},
        )
        self.assertEqual(Stage.objects.filter(deal=self.deal).count(), before)

    def test_withdrawn_emits_nothing(self):
        quote = make_commercial_quote(self.deal, lender=self.lender, term=None)
        before = Stage.objects.filter(deal=self.deal).count()
        self.client.post(
            reverse("crm:quote_update", args=[quote.pk]),
            {"lender": str(self.lender.pk), "status": "withdrawn"},
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, "withdrawn")
        self.assertEqual(Stage.objects.filter(deal=self.deal).count(), before)

    def test_asset_finance_quote_emits_no_stage(self):
        from .factories import make_rate_band

        af_deal = make_deal(owner=self.staff)
        make_participation(af_deal, amount=20_000)
        band = make_rate_band(term_months=60)
        before = Stage.objects.filter(deal=af_deal).count()
        response = self.client.post(
            reverse("crm:quote_create") + f"?deal={af_deal.pk}",
            {"term": "60", "rate": str(band.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Stage.objects.filter(deal=af_deal).count(), before)
        self.assertEqual(af_deal.quotes.get().status, "")


class TermsNotifyClientTests(TestCase):
    """Mirror of test_proposal_notify for Terms rows."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = _commercial_deal(cls.staff, amount="100000.00")
        cls.customer = cls.deal.customer
        cls.lender = make_organisation(name="Approving Bank")
        cls.terms = make_commercial_quote(
            cls.deal, lender=cls.lender, term=300, interest_rate="9.50",
            status="approved", lender_reference="AB-77",
        )
        cls.deal.selected_quote = cls.terms
        cls.deal.save(update_fields=["selected_quote"])

    def setUp(self):
        self.client.force_login(self.staff)

    def _post(self, quote=None):
        return self.client.post(reverse("crm:quote_notify", args=[(quote or self.terms).pk]))

    def test_notify_sends_email_with_details_and_records_send(self):
        response = self._post()
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.customer.email])
        self.assertIn("Approving Bank", message.subject)
        self.assertIn("AB-77", message.body)
        self.assertIn("300 months", message.body)
        self.assertIn("£873.70", message.body)

        self.terms.refresh_from_db()
        self.assertIsNotNone(self.terms.notified_at)
        latest = Stage.objects.filter(deal=self.deal).order_by("-occurred_at", "-pk").first()
        self.assertEqual(latest.name, Stage.Name.CLIENT_NOTIFIED)
        self.assertEqual(latest.organisation, self.lender)

    def test_notify_cannot_be_repeated(self):
        self._post()
        response = self._post()
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 1)

    def test_notify_rejected_when_not_approved(self):
        pending = make_commercial_quote(self.deal, term=None, status="submitted")
        self.deal.selected_quote = pending
        self.deal.save(update_fields=["selected_quote"])
        response = self._post(pending)
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 0)

    def test_notify_rejected_when_not_selected(self):
        other = make_commercial_quote(
            self.deal, term=240, interest_rate="9.00", status="approved",
        )
        response = self._post(other)
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 0)
        other.refresh_from_db()
        self.assertIsNone(other.notified_at)

    def test_notify_rejected_on_asset_finance_quote(self):
        af_deal = make_deal(owner=self.staff)
        make_participation(af_deal, amount=10_000)
        quote = make_quote(af_deal)
        af_deal.selected_quote = quote
        af_deal.save(update_fields=["selected_quote"])
        response = self.client.post(reverse("crm:quote_notify", args=[quote.pk]), follow=True)
        self.assertRedirects(response, af_deal.get_absolute_url())
        self.assertContains(response, "Only terms on a commercial deal")
        self.assertEqual(len(mail.outbox), 0)


class CommissionInvoiceFromTermsTests(TestCase):
    """The commission flows read the accepted lender off the selected Terms
    row on commercial deals (the selected Proposal on Asset Finance ones)."""

    @classmethod
    def setUpTestData(cls):
        cls.finance = make_associate(is_finance=True)

    def setUp(self):
        self.client.force_login(self.finance)

    def _deal_with_terms(self, **extra):
        deal = _commercial_deal(self.finance, amount="50000.00", **extra)
        lender = make_organisation(name="Big Lender Ltd")
        terms = make_commercial_quote(
            deal, lender=lender, term=120, interest_rate="7.50",
            status="approved", lender_reference="BL-4242",
        )
        deal.selected_quote = terms
        deal.save(update_fields=["selected_quote"])
        return deal

    def test_accepted_lender_terms_by_deal_type(self):
        deal = self._deal_with_terms()
        self.assertEqual(deal.accepted_lender_terms.lender.name, "Big Lender Ltd")
        self.assertEqual(deal.accepted_lender_terms.reference, "BL-4242")
        self.assertIsNone(_commercial_deal(self.finance).accepted_lender_terms)
        self.assertIsNone(make_deal(owner=self.finance).accepted_lender_terms)

    def test_request_commission_invoice_names_lender_and_reference(self):
        deal = self._deal_with_terms(commission=Decimal("1200.00"))
        with self.settings(ACCOUNTS_EMAILS=["accounts@example.com"]):
            response = self.client.post(
                reverse("crm:deal_request_commission_invoice", args=[deal.pk]),
            )
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Big Lender Ltd", body)
        self.assertIn("BL-4242", body)

    def test_request_commission_invoice_needs_selected_terms(self):
        deal = _commercial_deal(self.finance)
        response = self.client.post(
            reverse("crm:deal_request_commission_invoice", args=[deal.pk]), follow=True,
        )
        self.assertContains(response, "Select the terms")
        self.assertEqual(len(mail.outbox), 0)

    def test_raise_invoice_prefills_from_terms(self):
        make_xero_connection()
        deal = self._deal_with_terms(commission=Decimal("1200.00"))
        response = self.client.get(reverse("crm:deal_raise_invoice", args=[deal.pk]))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["contact_name"], "Big Lender Ltd")
        self.assertEqual(form.initial["reference"], f"{deal.name} · BL-4242")

    def test_raise_invoice_needs_selected_terms(self):
        make_xero_connection()
        deal = _commercial_deal(self.finance)
        response = self.client.get(reverse("crm:deal_raise_invoice", args=[deal.pk]), follow=True)
        self.assertContains(response, "Select the terms")


class TermLessRowModelTests(TestCase):
    """A Terms row without a term yet must never crash the derived values."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = _commercial_deal(
            cls.staff, amount="100000.00", first_payment_date=date(2027, 1, 1),
        )
        cls.pending = make_commercial_quote(cls.deal, term=None, status="submitted")
        cls.deal.selected_quote = cls.pending
        cls.deal.save(update_fields=["selected_quote"])

    def test_derived_values_are_none(self):
        self.assertIsNone(self.pending.priced_term)
        self.assertIsNone(self.pending.monthly_payment)
        self.assertIsNone(self.pending.apr)
        self.assertIsNone(self.pending.flat_rate)
        self.assertIsNone(self.deal.maturity_date)
        self.assertIsNone(self.deal.repayment_schedule)
        self.assertIn("term TBC", str(self.pending))

    def test_customer_quotes_filters_to_approved(self):
        approved = make_commercial_quote(
            self.deal, term=300, interest_rate="9.50", status="approved",
        )
        self.assertEqual(self.deal.customer_quotes, [approved])
        af_deal = make_deal(owner=self.staff)
        make_participation(af_deal, amount=10_000)
        quote = make_quote(af_deal)
        self.assertEqual(af_deal.customer_quotes, [quote])

    def test_terms_form_accepts_the_row(self):
        form = TermsForm(instance=self.pending)
        self.assertEqual(form.initial["status"], LenderStatus.SUBMITTED)
        self.assertIsNone(form.initial["term"])


class TermsPortalChecklistTests(TestCase):
    def test_no_choose_item_until_terms_approved(self):
        from crm.portal_checklist import outstanding_items

        staff = make_associate()
        deal = _commercial_deal(staff, amount="100000.00")
        make_commercial_quote(deal, term=None, status="submitted")
        labels = [item.label for item in outstanding_items(deal)]
        self.assertNotIn("Choose your quote", labels)
        make_commercial_quote(deal, term=300, interest_rate="9.50", status="approved")
        deal = Deal.objects.get(pk=deal.pk)
        labels = [item.label for item in outstanding_items(deal)]
        self.assertIn("Choose your quote", labels)
