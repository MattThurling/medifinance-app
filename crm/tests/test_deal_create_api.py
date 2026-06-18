"""Public deal-create API — `POST /api/deals/` with bearer auth.

Auth, validation, and the full side-effect chain: contact reuse/create,
introducer from the key, participation, auto-quotes, document requests, and
the staff notification email."""

from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import SiteSettings
from crm.api import RATE_LIMIT_HOUR_MAX
from crm.models import Contact, Deal, Document

from .factories import make_api_key, make_contact, make_organisation, make_rate_band

VALID_BODY = {
    "name": "Dental chair refit",
    "amount": 25_000,
    "first_name": "Jane",
    "last_name": "Smith",
    "email": "jane@example.com",
    "ltd": True,
}


class DealCreateApiAuthTests(TestCase):
    def _post(self, **headers):
        return self.client.post(
            reverse("crm_api:deal_list"),
            data=VALID_BODY,
            content_type="application/json",
            **headers,
        )

    def test_missing_authorization_header_returns_401(self):
        response = self._post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_unknown_token_returns_401(self):
        response = self._post(HTTP_AUTHORIZATION="Bearer mfk_does-not-exist")
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_returns_401(self):
        _, raw = make_api_key(is_active=False)
        response = self._post(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 401)

    def test_no_deal_is_created_on_auth_failure(self):
        self._post()
        self.assertEqual(Deal.objects.count(), 0)


class DealCreateApiValidationTests(TestCase):
    """Reject every bad input with a JSON 400, never a 500."""

    @classmethod
    def setUpTestData(cls):
        _, cls.raw = make_api_key()

    def _post(self, body):
        return self.client.post(
            reverse("crm_api:deal_list"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            reverse("crm_api:deal_list"),
            data="not-json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_json")

    def test_each_field_is_required(self):
        for field in VALID_BODY:
            with self.subTest(field=field):
                body = {k: v for k, v in VALID_BODY.items() if k != field}
                response = self._post(body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "missing_field")
                self.assertIn(field, response.json()["detail"])

    def test_zero_amount_returns_400(self):
        response = self._post({**VALID_BODY, "amount": 0})
        self.assertEqual(response.status_code, 400)

    def test_invalid_email_returns_400(self):
        response = self._post({**VALID_BODY, "email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json()["detail"])

    def test_non_boolean_ltd_returns_400(self):
        response = self._post({**VALID_BODY, "ltd": "yes"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("ltd", response.json()["detail"])

    def test_blank_name_returns_400(self):
        response = self._post({**VALID_BODY, "name": "   "})
        self.assertEqual(response.status_code, 400)

    def test_nothing_is_created_on_validation_failure(self):
        self._post({**VALID_BODY, "ltd": "yes"})
        self.assertEqual(Deal.objects.count(), 0)
        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(NOTIFY_EMAILS=["staff@medi-finance.co.uk"])
class DealCreateApiHappyPathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.integrator = make_organisation(name="Partner Portal Ltd")
        _, cls.raw = make_api_key(organisation=cls.integrator)

        cls.cheap = make_organisation(name="Cheap Lender")
        cls.dear = make_organisation(name="Dear Lender")
        # Two lenders at 60 months — only the cheaper one should be quoted.
        make_rate_band(organisation=cls.cheap, term_months=60,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="10.00")
        make_rate_band(organisation=cls.dear, term_months=60,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="14.00")
        # A second available term.
        make_rate_band(organisation=cls.dear, term_months=36,
                       min_amount=1_000, max_amount=1_000_000, yield_percent="12.00")
        # A band that does NOT cover the amount — its term must not be quoted.
        make_rate_band(organisation=cls.cheap, term_months=12,
                       min_amount=1_000, max_amount=5_000, yield_percent="9.00")

    def _post(self, body=None):
        return self.client.post(
            reverse("crm_api:deal_list"),
            data=body or VALID_BODY,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )

    def test_creates_deal_with_introducer_from_key(self):
        response = self._post()
        self.assertEqual(response.status_code, 201)
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertEqual(deal.name, "Dental chair refit")
        self.assertEqual(deal.introducer, self.integrator)
        self.assertIsNone(deal.owner)
        self.assertEqual(deal.funded_amount, Decimal("25000.00"))
        participation = deal.participations.get()
        self.assertEqual(participation.organisation, self.integrator)

    def test_creates_contact_from_payload(self):
        self._post()
        contact = Contact.objects.get(email="jane@example.com")
        self.assertEqual(contact.first_name, "Jane")
        self.assertEqual(contact.last_name, "Smith")
        self.assertEqual(Deal.objects.get().customer, contact)

    def test_reuses_existing_contact_by_email_case_insensitive(self):
        existing = make_contact(email="JANE@example.com")
        self._post()
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Deal.objects.get().customer, existing)

    def test_attaches_cheapest_quote_per_available_term(self):
        response = self._post()
        deal = Deal.objects.get(pk=response.json()["id"])
        quotes = list(deal.quotes.select_related("rate__organisation").order_by("term"))
        self.assertEqual(
            [(q.term, q.rate.organisation.name) for q in quotes],
            [(36, "Dear Lender"), (60, "Cheap Lender")],
        )
        # Same inputs as the quote-API golden, but API-created quotes default
        # to 3% commission, which uplifts the monthly payment.
        sixty = next(q for q in quotes if q.term == 60)
        self.assertEqual(sixty.commission_percent, Decimal("3"))
        self.assertEqual(str(sixty.monthly_payment), "542.59")

    def test_response_contains_deal_summary(self):
        response = self._post()
        self.assertEqual(
            set(response.json()),
            {"id", "name", "amount", "deduplicated"},
        )
        self.assertEqual(response.json()["amount"], "25000.00")
        self.assertFalse(response.json()["deduplicated"])

    def test_attaches_standard_document_requests(self):
        response = self._post()
        deal = Deal.objects.get(pk=response.json()["id"])
        names = set(deal.documents.values_list("name", flat=True))
        self.assertEqual(names, {
            "Last 3 months business bank statements",
            "Most recent financial accounts or tax returns",
        })
        self.assertTrue(all(
            d.status == Document.Status.REQUESTED for d in deal.documents.all()
        ))

    def test_sends_notification_email(self):
        response = self._post()
        # Two emails go out: staff notification + customer portal magic link.
        self.assertEqual(len(mail.outbox), 2)
        staff_email = next(m for m in mail.outbox if "staff@medi-finance.co.uk" in m.to)
        self.assertIn("Dental chair refit", staff_email.subject)
        self.assertIn("Partner Portal Ltd", staff_email.body)
        self.assertIn("jane@example.com", staff_email.body)
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertIn(deal.get_absolute_url(), staff_email.body)

    def test_sends_customer_portal_link_email(self):
        self._post()
        customer_email = next(m for m in mail.outbox if "jane@example.com" in m.to)
        self.assertIn("/m/", customer_email.body)

    def test_no_available_terms_still_creates_deal_with_no_quotes(self):
        response = self._post({**VALID_BODY, "amount": 5_000_000})
        self.assertEqual(response.status_code, 201)
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertEqual(deal.quotes.count(), 0)

    def test_get_without_session_still_returns_401_json(self):
        """The GET (staff list) surface on the same URL is untouched."""
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")


class DealCreateApiSafeguardTests(TestCase):
    """Spam-protection layers: global kill switch, per-customer dedup, rate limit."""

    @classmethod
    def setUpTestData(cls):
        cls.integrator = make_organisation(name="Partner Portal Ltd")
        _, cls.raw = make_api_key(organisation=cls.integrator)
        make_rate_band(organisation=make_organisation(name="Any Lender"),
                       term_months=60, min_amount=1_000, max_amount=1_000_000,
                       yield_percent="10.00")

    def _post(self, body=None, raw=None):
        return self.client.post(
            reverse("crm_api:deal_list"),
            data=body or VALID_BODY,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}",
        )

    # --- kill switch -------------------------------------------------------

    def test_kill_switch_off_returns_503_and_creates_nothing(self):
        settings_row = SiteSettings.get()
        settings_row.api_enabled = False
        settings_row.save()

        response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "api_disabled")
        self.assertEqual(Deal.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_kill_switch_back_on_resumes_creates(self):
        settings_row = SiteSettings.get()
        settings_row.api_enabled = False
        settings_row.save()
        self._post()  # 503
        settings_row.api_enabled = True
        settings_row.save()

        response = self._post()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Deal.objects.count(), 1)

    # --- dedup -------------------------------------------------------------

    def test_same_email_within_24h_returns_existing_deal(self):
        first = self._post()
        first_id = first.json()["id"]

        second = self._post({**VALID_BODY, "name": "A different name"})

        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(second.json()["id"], first_id)
        self.assertEqual(Deal.objects.count(), 1)

    def test_dedup_does_not_send_second_customer_email(self):
        self._post()
        mail.outbox.clear()

        self._post()

        # No second magic link to the customer.
        self.assertFalse(any("jane@example.com" in m.to for m in mail.outbox))

    def test_dedup_is_case_insensitive_on_email(self):
        first = self._post()
        second = self._post({**VALID_BODY, "email": "JANE@example.com"})
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(second.json()["id"], first.json()["id"])

    def test_dedup_is_per_introducer(self):
        # Another partner sees their own dedup namespace.
        other_org = make_organisation(name="Other Partner")
        _, other_raw = make_api_key(organisation=other_org)

        self._post()
        response = self._post(raw=other_raw)

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(Deal.objects.count(), 2)

    def test_dedup_window_expires_after_24h(self):
        first = self._post()
        deal = Deal.objects.get(pk=first.json()["id"])
        # Backdate the existing deal beyond the dedup window.
        Deal.objects.filter(pk=deal.pk).update(
            created_at=timezone.now() - timedelta(hours=25),
        )

        response = self._post()

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(Deal.objects.count(), 2)

    # --- rate limit --------------------------------------------------------

    def test_rate_limit_triggers_after_hour_max(self):
        # Burn through the hourly cap with different customer emails so dedup
        # doesn't short-circuit and consume the limit.
        for i in range(RATE_LIMIT_HOUR_MAX):
            response = self._post({**VALID_BODY, "email": f"c{i}@example.com"})
            self.assertEqual(response.status_code, 201)

        response = self._post({**VALID_BODY, "email": "one-too-many@example.com"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "rate_limited")
        self.assertEqual(Deal.objects.count(), RATE_LIMIT_HOUR_MAX)

    def test_rate_limit_is_per_key(self):
        # Saturate the first key.
        for i in range(RATE_LIMIT_HOUR_MAX):
            self._post({**VALID_BODY, "email": f"c{i}@example.com"})

        # A second key on a separate org is unaffected.
        other_org = make_organisation(name="Other Partner")
        _, other_raw = make_api_key(organisation=other_org)
        response = self._post(
            {**VALID_BODY, "email": "fresh@example.com"},
            raw=other_raw,
        )
        self.assertEqual(response.status_code, 201)

    def test_dedup_hits_do_not_count_against_rate_limit(self):
        # Smash the SAME customer many times — all should dedup, none should
        # eat into the rate limit budget.
        for _ in range(RATE_LIMIT_HOUR_MAX + 5):
            response = self._post()
            self.assertIn(response.status_code, (200, 201))

        # The next NEW customer should still go through.
        response = self._post({**VALID_BODY, "email": "fresh@example.com"})
        self.assertEqual(response.status_code, 201)

    # --- commission --------------------------------------------------------

    def test_quotes_default_to_3_percent_commission(self):
        response = self._post()
        deal = Deal.objects.get(pk=response.json()["id"])
        for quote in deal.quotes.all():
            self.assertEqual(quote.commission_percent, Decimal("3"))

    # --- attribution -------------------------------------------------------

    def test_deal_records_creating_api_key(self):
        response = self._post()
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertEqual(deal.created_via_api_key.prefix[:4], "mfk_")
