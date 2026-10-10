"""Partner API additions for guest and AI-agent submissions.

Covers the additive contract on `POST /api/deals/`: guest applications with
only name/email/company type, channel attribution, structured errors, amount
bounds, Idempotency-Key replay and conflict, the per-email rate limit,
sandbox keys, the audit log, the partner-flavoured customer email, and
`GET /api/applications/<reference>/status/`.
"""

from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.api import EMAIL_RATE_LIMIT_HOUR_MAX
from crm.models import Contact, Deal, PartnerApiRequest, Stage

from .factories import make_api_key, make_organisation, make_rate_band

GUEST_BODY = {
    "description": "ErgoDent Signature Surgery Package — Acme Ltd",
    "amount": 32_750,
    "first_name": "Jack",
    "last_name": "Jones",
    "email": "jack@jjdental.com",
    "company_type": "limited_company",
    "channel": "web-guest",
    "product_id": "chair-package",
    "asset_category": "surgery",
}

V1_BODY = {
    "name": "Dental chair refit",
    "amount": 25_000,
    "first_name": "Jane",
    "last_name": "Smith",
    "email": "jane@example.com",
    "ltd": True,
}


@override_settings(NOTIFY_EMAILS=["staff@medi-finance.co.uk"])
class PartnerApiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.partner = make_organisation(name="Acme Ltd")
        cls.key, cls.raw = make_api_key(organisation=cls.partner)
        make_rate_band(organisation=make_organisation(name="Any Lender"),
                       term_months=60, min_amount=1_000, max_amount=1_000_000,
                       yield_percent="10.00")

    def _post(self, body=None, raw=None, **headers):
        return self.client.post(
            reverse("crm_api:deal_list"),
            data=GUEST_BODY if body is None else body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}",
            **headers,
        )


class GuestApplicationTests(PartnerApiBase):
    """A first-time applicant with no account, via a guest or agent channel."""

    def test_guest_can_start_an_application_with_description_and_company_type(self):
        response = self._post()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        deal = Deal.objects.get(pk=body["id"])
        self.assertEqual(deal.name, GUEST_BODY["description"])
        self.assertEqual(deal.customer.email, "jack@jjdental.com")
        self.assertEqual(deal.introducer, self.partner)
        self.assertEqual(deal.channel, "web-guest")
        self.assertEqual(deal.company_type, "limited_company")
        self.assertEqual(deal.partner_product_id, "chair-package")
        self.assertEqual(deal.asset_category, "surgery")
        self.assertEqual(body["application_reference"], f"MF-{deal.pk}")
        self.assertEqual(body["status"], "awaiting_customer")
        self.assertIn("jack@jjdental.com", body["message"])

    def test_message_never_contains_finance_terms(self):
        body = self._post().json()
        for banned in ("APR", "%", "per month", "monthly"):
            self.assertNotIn(banned, body["message"])

    def test_v1_payload_still_works_unchanged(self):
        response = self._post(V1_BODY)
        self.assertEqual(response.status_code, 201)
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertEqual(deal.channel, "")
        # `ltd: true` is recorded as a limited company so reporting is uniform.
        self.assertEqual(deal.company_type, "limited_company")

    def test_sole_trader_maps_to_not_ltd(self):
        response = self._post({**GUEST_BODY, "company_type": "sole_trader"})
        self.assertEqual(response.status_code, 201)
        staff_email = next(m for m in mail.outbox if "staff@medi-finance.co.uk" in m.to)
        self.assertIn("Ltd:        No", staff_email.body)

    def test_agent_client_is_stored_as_free_text(self):
        response = self._post({**GUEST_BODY, "channel": "mcp", "agent_client": "claude"})
        deal = Deal.objects.get(pk=response.json()["id"])
        self.assertEqual(deal.channel, "mcp")
        self.assertEqual(deal.agent_client, "claude")

    def test_repeat_email_reuses_contact_not_a_duplicate_person(self):
        self._post()
        Deal.objects.update(created_at=timezone.now() - timedelta(days=2))  # outside dedup
        self._post({**GUEST_BODY, "first_name": "Jack", "last_name": "Jones"})
        self.assertEqual(Contact.objects.filter(email__iexact="jack@jjdental.com").count(), 1)
        self.assertEqual(Deal.objects.count(), 2)

    def test_customer_email_explains_partner_and_is_safe_to_ignore(self):
        self._post()
        customer_email = next(m for m in mail.outbox if "jack@jjdental.com" in m.to)
        self.assertIn("through Acme Ltd", customer_email.body)
        self.assertIn("If you didn't request this, you can ignore this email",
                      customer_email.body)
        html = customer_email.alternatives[0][0]
        self.assertIn("through Acme Ltd", html)
        self.assertIn("you can ignore this email", html)

    def test_staff_sent_link_email_keeps_original_wording(self):
        from accounts.emails import send_magic_link_email
        send_magic_link_email(
            to_email="x@example.com", link_url="https://example.com/m/abc",
            deal_name="Deal", owner_name="Sam", expires_at=timezone.now(),
        )
        self.assertIn("Sam at Medifinance has shared your application", mail.outbox[-1].body)
        self.assertNotIn("ignore this email", mail.outbox[-1].body)


class StructuredErrorTests(PartnerApiBase):
    def test_missing_required_field_names_the_field(self):
        body = {k: v for k, v in GUEST_BODY.items() if k != "email"}
        response = self._post(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"], [
            {"field": "email", "code": "required", "message": "`email` is required."},
        ])
        # Flat v1 shape is still present.
        self.assertEqual(response.json()["error"], "missing_field")

    def test_invalid_email_is_a_field_error(self):
        response = self._post({**GUEST_BODY, "email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        err = response.json()["errors"][0]
        self.assertEqual((err["field"], err["code"]), ("email", "invalid"))

    def test_unknown_channel_lists_allowed_values(self):
        response = self._post({**GUEST_BODY, "channel": "carrier-pigeon"})
        self.assertEqual(response.status_code, 400)
        err = response.json()["errors"][0]
        self.assertEqual(err["field"], "channel")
        self.assertIn("web, web-guest, mcp, api", err["message"])

    def test_unknown_company_type_lists_allowed_values(self):
        response = self._post({**GUEST_BODY, "company_type": "llc"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("limited_company, sole_trader, partnership, other",
                      response.json()["errors"][0]["message"])

    def test_neither_ltd_nor_company_type_is_a_400_on_ltd(self):
        body = {k: v for k, v in GUEST_BODY.items() if k != "company_type"}
        response = self._post(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "ltd")

    def test_amount_below_minimum_is_422(self):
        response = self._post({**GUEST_BODY, "amount": 500})
        self.assertEqual(response.status_code, 422)
        err = response.json()["errors"][0]
        self.assertEqual((err["field"], err["code"]), ("amount", "out_of_range"))
        self.assertIn("£1,000", err["message"])
        self.assertEqual(Deal.objects.count(), 0)

    def test_amount_above_maximum_is_422(self):
        response = self._post({**GUEST_BODY, "amount": 50_000_000})
        self.assertEqual(response.status_code, 422)

    def test_malformed_json_has_structured_error_without_field(self):
        response = self.client.post(
            reverse("crm_api:deal_list"), data="{", content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], None)
        self.assertEqual(response.json()["errors"][0]["code"], "invalid_json")

    def test_rejections_are_logged(self):
        self._post({**GUEST_BODY, "email": "not-an-email"})
        row = PartnerApiRequest.objects.get()
        self.assertEqual(row.outcome, PartnerApiRequest.Outcome.REJECTED)
        self.assertEqual(row.status_code, 400)
        self.assertEqual(row.channel, "web-guest")
        self.assertEqual(row.organisation, self.partner)


class IdempotencyTests(PartnerApiBase):
    def test_retry_with_same_key_and_body_returns_original_and_sends_nothing(self):
        first = self._post(HTTP_IDEMPOTENCY_KEY="abc-123")
        self.assertEqual(first.status_code, 201)
        sent_before = len(mail.outbox)

        second = self._post(HTTP_IDEMPOTENCY_KEY="abc-123")

        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(Deal.objects.count(), 1)
        self.assertEqual(len(mail.outbox), sent_before)
        outcomes = list(PartnerApiRequest.objects.order_by("pk").values_list("outcome", flat=True))
        self.assertEqual(outcomes, ["created", "replayed"])
        replay = PartnerApiRequest.objects.latest("pk")
        self.assertEqual(replay.replay_of, PartnerApiRequest.objects.earliest("pk"))
        self.assertEqual(replay.deal, Deal.objects.get())

    def test_same_key_different_body_is_409(self):
        self._post(HTTP_IDEMPOTENCY_KEY="abc-123")
        response = self._post({**GUEST_BODY, "amount": 40_000}, HTTP_IDEMPOTENCY_KEY="abc-123")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "idempotency_key_reused")
        self.assertEqual(response.json()["errors"][0]["field"], "Idempotency-Key")
        self.assertEqual(Deal.objects.count(), 1)

    def test_keys_are_scoped_per_partner(self):
        self._post(HTTP_IDEMPOTENCY_KEY="abc-123")
        _, other_raw = make_api_key(organisation=make_organisation(name="Other"))
        response = self._post(raw=other_raw, HTTP_IDEMPOTENCY_KEY="abc-123")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(Deal.objects.count(), 2)

    def test_failed_attempt_is_not_replayed(self):
        bad = self._post({**GUEST_BODY, "email": "nope"}, HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(bad.status_code, 400)
        good = self._post(HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(good.status_code, 201)

    def test_key_expires_after_window(self):
        self._post(HTTP_IDEMPOTENCY_KEY="k1")
        PartnerApiRequest.objects.update(created_at=timezone.now() - timedelta(hours=25))
        Deal.objects.update(created_at=timezone.now() - timedelta(hours=25))
        response = self._post(HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Deal.objects.count(), 2)

    def test_overlong_key_is_400(self):
        response = self._post(HTTP_IDEMPOTENCY_KEY="x" * 300)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "Idempotency-Key")

    def test_dedup_response_is_replayable_too(self):
        self._post()  # creates
        dedup = self._post(HTTP_IDEMPOTENCY_KEY="k2")
        self.assertEqual(dedup.status_code, 200)
        self.assertTrue(dedup.json()["deduplicated"])
        again = self._post(HTTP_IDEMPOTENCY_KEY="k2")
        self.assertEqual(again.json(), dedup.json())
        self.assertEqual(PartnerApiRequest.objects.latest("pk").outcome, "replayed")


class PerEmailRateLimitTests(PartnerApiBase):
    def test_same_email_across_partners_is_capped_per_hour(self):
        # Dedup is per-introducer, so N partners can each create one deal for
        # the same email — until the per-email cap bites.
        raws = [self.raw]
        for i in range(EMAIL_RATE_LIMIT_HOUR_MAX):
            _, raw = make_api_key(organisation=make_organisation(name=f"Partner {i}"))
            raws.append(raw)
        for raw in raws[:EMAIL_RATE_LIMIT_HOUR_MAX]:
            self.assertEqual(self._post(raw=raw).status_code, 201)

        response = self._post(raw=raws[EMAIL_RATE_LIMIT_HOUR_MAX])

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "email_rate_limited")
        self.assertEqual(response.json()["errors"][0]["field"], "email")
        self.assertEqual(Deal.objects.count(), EMAIL_RATE_LIMIT_HOUR_MAX)
        self.assertEqual(PartnerApiRequest.objects.latest("pk").outcome, "rate_limited")

    def test_per_key_limit_error_is_structured(self):
        for i in range(5):
            self._post({**GUEST_BODY, "email": f"c{i}@example.com"})
        response = self._post({**GUEST_BODY, "email": "c99@example.com"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["errors"][0]["code"], "rate_limited")


class SandboxTests(PartnerApiBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sandbox_key, cls.sandbox_raw = make_api_key(
            organisation=cls.partner, is_sandbox=True,
        )

    def test_sandbox_validates_and_returns_fake_reference_without_side_effects(self):
        response = self._post(raw=self.sandbox_raw)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["sandbox"])
        self.assertTrue(body["application_reference"].startswith("MF-SANDBOX-"))
        self.assertEqual(body["status"], "sandbox")
        self.assertIsNone(body["id"])
        self.assertEqual(Deal.objects.count(), 0)
        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
        row = PartnerApiRequest.objects.get()
        self.assertEqual(row.outcome, PartnerApiRequest.Outcome.SANDBOX)
        self.assertEqual(row.api_key, self.sandbox_key)

    def test_sandbox_still_rejects_invalid_input(self):
        response = self._post({**GUEST_BODY, "email": "nope"}, raw=self.sandbox_raw)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "email")

    def test_sandbox_honours_idempotency(self):
        first = self._post(raw=self.sandbox_raw, HTTP_IDEMPOTENCY_KEY="s1")
        second = self._post(raw=self.sandbox_raw, HTTP_IDEMPOTENCY_KEY="s1")
        self.assertEqual(first.json(), second.json())

    def test_sandbox_status_lookup(self):
        reference = self._post(raw=self.sandbox_raw).json()["application_reference"]
        response = self.client.get(
            reverse("crm_api:application_status", args=[reference]),
            HTTP_AUTHORIZATION=f"Bearer {self.sandbox_raw}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sandbox")


class ApplicationStatusTests(PartnerApiBase):
    def _get(self, reference, raw=None):
        return self.client.get(
            reverse("crm_api:application_status", args=[reference]),
            HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}",
        )

    def test_own_application_is_awaiting_customer(self):
        reference = self._post().json()["application_reference"]
        response = self._get(reference)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["application_reference"], reference)
        self.assertEqual(body["status"], "awaiting_customer")
        self.assertEqual(body["stage"], "application")
        self.assertEqual(body["channel"], "web-guest")
        self.assertEqual(body["amount"], "32750.00")

    def test_stage_moves_status_to_in_progress_and_closed(self):
        body = self._post().json()
        deal = Deal.objects.get(pk=body["id"])
        Stage.objects.create(deal=deal, name=Stage.Name.QUOTING)
        self.assertEqual(self._get(body["application_reference"]).json()["status"], "in_progress")
        Stage.objects.create(deal=deal, name=Stage.Name.LOST)
        self.assertEqual(self._get(body["application_reference"]).json()["status"], "closed")

    def test_other_partners_application_is_404(self):
        reference = self._post().json()["application_reference"]
        _, other_raw = make_api_key(organisation=make_organisation(name="Other"))
        response = self._get(reference, raw=other_raw)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["errors"][0]["code"], "not_found")

    def test_garbage_reference_is_404(self):
        self.assertEqual(self._get("nonsense").status_code, 404)
        self.assertEqual(self._get("MF-999999").status_code, 404)

    def test_requires_bearer_token(self):
        response = self.client.get(reverse("crm_api:application_status", args=["MF-1"]))
        self.assertEqual(response.status_code, 401)


class AuditLogTests(PartnerApiBase):
    def test_every_outcome_is_logged_with_attribution(self):
        self._post(HTTP_IDEMPOTENCY_KEY="k")                     # created
        self._post({**GUEST_BODY, "name": "x"})                   # deduplicated
        self._post({**GUEST_BODY, "amount": 1})                   # rejected (422)
        rows = list(PartnerApiRequest.objects.order_by("pk"))
        self.assertEqual([r.outcome for r in rows], ["created", "deduplicated", "rejected"])
        self.assertEqual(rows[0].idempotency_key, "k")
        self.assertEqual(rows[0].deal, Deal.objects.get())
        self.assertEqual(rows[1].deal, Deal.objects.get())
        self.assertTrue(all(r.organisation == self.partner for r in rows))
        self.assertTrue(all(r.email == "jack@jjdental.com" for r in rows))
        self.assertEqual(rows[0].response_body["application_reference"],
                         f"MF-{Deal.objects.get().pk}")

    def test_kill_switch_is_logged(self):
        from accounts.models import SiteSettings
        row = SiteSettings.get()
        row.api_enabled = False
        row.save()
        self._post()
        self.assertEqual(PartnerApiRequest.objects.get().outcome, "disabled")
