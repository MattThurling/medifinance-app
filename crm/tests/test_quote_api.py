"""Public quote API — `POST /api/quotes/` with bearer auth.

Auth, validation, and happy path. The API is the entire integrator surface;
any drift here breaks a partner integration in production."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from crm import pricing

from .factories import make_api_key, make_organisation, make_rate_band


class QuoteApiAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A valid band so a well-formed request would otherwise succeed —
        # we want auth failures to surface, not "no rate available".
        make_rate_band(term_months=60, min_amount=1_000, max_amount=1_000_000,
                       yield_percent="10.00")

    def _post(self, **headers):
        return self.client.post(
            reverse("crm_api:quote"),
            data={"amount": 25_000, "term_months": 60},
            content_type="application/json",
            **headers,
        )

    def test_missing_authorization_header_returns_401(self):
        response = self._post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_wrong_scheme_returns_401(self):
        response = self._post(HTTP_AUTHORIZATION="Basic deadbeef")
        self.assertEqual(response.status_code, 401)

    def test_unknown_token_returns_401(self):
        response = self._post(HTTP_AUTHORIZATION="Bearer mfk_does-not-exist")
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_returns_401(self):
        _, raw = make_api_key(is_active=False)
        response = self._post(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 401)

    def test_valid_key_returns_200(self):
        _, raw = make_api_key()
        response = self._post(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 200)


class QuoteApiValidationTests(TestCase):
    """Reject every bad input with a JSON 400, never a 500."""

    @classmethod
    def setUpTestData(cls):
        _, cls.raw = make_api_key()
        make_rate_band(term_months=60, min_amount=1_000, max_amount=1_000_000,
                       yield_percent="10.00")

    def _post(self, body):
        return self.client.post(
            reverse("crm_api:quote"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            reverse("crm_api:quote"),
            data="not-json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_json")

    def test_missing_amount_returns_400(self):
        response = self._post({"term_months": 60})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_field")
        self.assertIn("amount", response.json()["detail"])

    def test_missing_term_returns_400(self):
        response = self._post({"amount": 25_000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_field")
        self.assertIn("term_months", response.json()["detail"])

    def test_zero_amount_returns_400(self):
        response = self._post({"amount": 0, "term_months": 60})
        self.assertEqual(response.status_code, 400)

    def test_negative_amount_returns_400(self):
        response = self._post({"amount": -1, "term_months": 60})
        self.assertEqual(response.status_code, 400)

    def test_non_integer_term_returns_400(self):
        response = self._post({"amount": 25_000, "term_months": 60.5})
        self.assertEqual(response.status_code, 400)

    def test_negative_commission_returns_400(self):
        response = self._post({
            "amount": 25_000, "term_months": 60, "commission_percent": -1,
        })
        self.assertEqual(response.status_code, 400)


class QuoteApiHappyPathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _, cls.raw = make_api_key()
        cls.cheap_lender = make_organisation(name="Cheap Lender")
        cls.dear_lender = make_organisation(name="Dear Lender")
        make_rate_band(organisation=cls.cheap_lender, term_months=60,
                       min_amount=1_000, max_amount=1_000_000,
                       yield_percent="10.00")
        make_rate_band(organisation=cls.dear_lender, term_months=60,
                       min_amount=1_000, max_amount=1_000_000,
                       yield_percent="14.00")

    def _post(self, body):
        return self.client.post(
            reverse("crm_api:quote"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
        )

    def test_returns_quote_against_cheapest_lender(self):
        response = self._post({"amount": 25_000, "term_months": 60})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["lender"], "Cheap Lender")
        self.assertEqual(data["amount"], "25000.00")
        self.assertEqual(data["term_months"], 60)
        self.assertEqual(data["commission_percent"], "0.00")
        # Pinned via test_pricing's golden values — same inputs, same answers.
        self.assertEqual(data["monthly_payment"], "526.79")
        self.assertEqual(data["apr"], "9.64")
        self.assertEqual(data["flat_rate"], "5.29")
        self.assertEqual(data["total_interest"], "6607.40")

    def test_commission_grosses_up_monthly_payment(self):
        base = self._post({"amount": 25_000, "term_months": 60}).json()
        grossed = self._post({
            "amount": 25_000, "term_months": 60, "commission_percent": 1.5,
        }).json()
        self.assertEqual(grossed["monthly_payment"], "534.69")
        self.assertEqual(grossed["commission_percent"], "1.50")
        self.assertGreater(
            Decimal(grossed["monthly_payment"]),
            Decimal(base["monthly_payment"]),
        )

    def test_no_active_band_returns_404(self):
        # No band covers £999,999 in our setup (max_amount is 1_000_000 — fine —
        # but term 12 has no bands at all).
        response = self._post({"amount": 25_000, "term_months": 12})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "no_rate_available")

    def test_amount_above_every_band_returns_404(self):
        response = self._post({"amount": 5_000_000, "term_months": 60})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "no_rate_available")

    def test_uses_pricing_module_directly(self):
        """Sanity check: the values the API returns are exactly what calling
        the pricing functions directly would produce — no drift between the
        internal Quote model and the public surface."""
        band = pricing.cheapest_rate_band(term_months=60, amount=25_000)
        direct_monthly = pricing.monthly_payment(
            principal=Decimal("25000"), rate_band=band, term_months=60,
        )
        response = self._post({"amount": 25_000, "term_months": 60})
        self.assertEqual(response.json()["monthly_payment"], str(direct_monthly))

    def test_last_used_at_is_stamped_on_successful_call(self):
        from accounts.models import ApiKey
        instance, raw = make_api_key()
        self.assertIsNone(instance.last_used_at)
        response = self.client.post(
            reverse("crm_api:quote"),
            data={"amount": 25_000, "term_months": 60},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertEqual(response.status_code, 200)
        instance.refresh_from_db()
        self.assertIsNotNone(instance.last_used_at)
