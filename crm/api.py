"""JSON APIs.

Two surfaces live here:

* **Internal extension API** (`StaffApiView` + `DealListApi` / `DealDetailApi`):
  read-only, session-cookie auth via the staff member's CRM login. Consumed by
  the Medifinance browser extension to flat-fill partner application forms.

* **Public quote API** (`BearerApiView` + `QuoteApi`): stateless POST,
  `Authorization: Bearer <api_key>` auth (see `accounts.models.ApiKey`).
  Takes amount + term, returns the best-available quote.

Everything here returns JSON 401/403/4xx rather than HTML redirects, so the
client can show a sensible message instead of trying to parse a login page.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from accounts.models import ApiKey

from . import pricing
from .models import Deal


def _money(value: Decimal | None) -> str | None:
    """Decimal -> fixed 2dp string (JSON-safe), or None."""
    return f"{value:.2f}" if value is not None else None


class StaffApiView(View):
    """Base view: require an authenticated staff member, else JSON 401/403."""

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return JsonResponse(
                {"error": "not_authenticated",
                 "detail": "Sign in to the Medifinance CRM, then try again."},
                status=401,
            )
        if not getattr(user, "is_staff_member", False):
            return JsonResponse(
                {"error": "forbidden",
                 "detail": "This account doesn't have access to deal data."},
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)


def _deal_list_row(deal: Deal) -> dict:
    customer = deal.customer
    return {
        "id": deal.pk,
        "name": deal.name,
        "customer": customer.full_name or customer.email,
        "business": deal.organisation.name if deal.organisation else None,
        "funded_amount": _money(deal.funded_amount),
    }


def _deal_fill_payload(deal: Deal) -> dict:
    """Flat, partner-agnostic view of a deal for form filling. Field maps in the
    extension pick whichever of these values a given partner form needs."""
    customer = deal.customer
    org = deal.organisation
    quote = deal.selected_quote

    return {
        "id": deal.pk,
        "name": deal.name,
        "reference": f"MF-{deal.pk}",
        "funded_amount": _money(deal.funded_amount),
        "broker": {
            "name": deal.owner.full_name,
            "email": deal.owner.email,
        },
        "business": {
            "name": org.name,
            "address_line1": org.address_line1,
            "address_line2": org.address_line2,
            "city": org.address_city,
            "county": org.address_county,
            "postcode": org.address_postcode,
        } if org else None,
        "applicant": {
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "full_name": customer.full_name,
            "email": customer.email,
            "phone": customer.phone,
            "date_of_birth": customer.date_of_birth.isoformat() if customer.date_of_birth else None,
            "address_line1": customer.home_address_line1,
            "address_line2": customer.home_address_line2,
            "city": customer.home_address_city,
            "county": customer.home_address_county,
            "postcode": customer.home_address_postcode,
        },
        "quote": {
            "apr": _money(quote.rate.yield_percent) if quote and quote.rate_id else None,
            "term_months": quote.term,
            "monthly_payment": _money(quote.monthly_payment),
        } if quote else None,
    }


class DealListApi(StaffApiView):
    """GET /api/deals/  -> recent deals + the signed-in user. Optional ?q= search."""

    def get(self, request):
        qs = (
            Deal.objects
            .select_related("customer", "organisation", "owner")
            .order_by("-created_at")
        )
        q = request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(customer__first_name__icontains=q)
                | Q(customer__last_name__icontains=q)
                | Q(organisation__name__icontains=q)
            )
        user = request.user
        return JsonResponse({
            "user": {"name": user.full_name, "email": user.email, "role": user.role},
            "deals": [_deal_list_row(d) for d in qs[:50]],
        })


class DealDetailApi(StaffApiView):
    """GET /api/deals/<pk>/  -> the flat fill payload for one deal."""

    def get(self, request, pk):
        try:
            deal = (
                Deal.objects
                .select_related(
                    "customer", "organisation", "owner",
                    "selected_quote", "selected_quote__rate",
                )
                .get(pk=pk)
            )
        except Deal.DoesNotExist:
            return JsonResponse({"error": "not_found"}, status=404)
        return JsonResponse(_deal_fill_payload(deal))


# -- Public quote API -------------------------------------------------------

@method_decorator(csrf_exempt, name="dispatch")
class BearerApiView(View):
    """Base view: require a valid `Authorization: Bearer <key>` against an
    active `ApiKey`, else JSON 401.

    Auth is header-only — there's no session cookie to protect, so the CSRF
    middleware is exempted (it'd reject all integrator POSTs otherwise)."""

    def dispatch(self, request, *args, **kwargs):
        raw = self._extract_bearer(request)
        api_key = ApiKey.authenticate(raw)
        if api_key is None:
            return JsonResponse(
                {"error": "not_authenticated",
                 "detail": "Send `Authorization: Bearer <api_key>` with a valid, active key."},
                status=401,
            )
        request.api_key = api_key
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _extract_bearer(request) -> str | None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        return token.strip()


class QuoteApi(BearerApiView):
    """POST /api/quotes/  -> the cheapest available quote for an amount + term.

    Request body (JSON):
        {
          "amount": 25000,
          "term_months": 60,
          "commission_percent": 1.5   // optional, defaults to 0
        }

    Response (200):
        {
          "amount": "25000.00",
          "term_months": 60,
          "commission_percent": "0.00",
          "lender": "BNP Paribas",
          "monthly_payment": "534.12",
          "apr": "11.46",
          "flat_rate": "5.78",
          "total_interest": "7047.20"
        }

    Errors (always JSON):
        400 — malformed body / missing field / non-positive amount or term
        401 — bad/missing bearer token
        404 — no active rate band covers this amount + term
    """

    def post(self, request):
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return _bad_request("invalid_json", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            return _bad_request("invalid_json", "Request body must be a JSON object.")

        amount, err = _parse_decimal(payload, "amount", positive=True)
        if err:
            return err
        term_months, err = _parse_int(payload, "term_months", positive=True)
        if err:
            return err
        commission_percent, err = _parse_decimal(
            payload, "commission_percent", positive=False, required=False, default=Decimal("0"),
        )
        if err:
            return err
        if commission_percent < 0:
            return _bad_request("invalid_field", "commission_percent must not be negative.")

        band = pricing.cheapest_rate_band(term_months=term_months, amount=amount)
        if band is None:
            return JsonResponse(
                {"error": "no_rate_available",
                 "detail": "No active rate band covers this amount and term."},
                status=404,
            )

        monthly = pricing.monthly_payment(
            principal=amount,
            rate_band=band,
            term_months=term_months,
            commission_percent=commission_percent,
        )
        apr_value = pricing.apr(principal=amount, monthly_payment=monthly, term_months=term_months)
        flat = pricing.flat_rate(principal=amount, monthly_payment=monthly, term_months=term_months)
        interest = pricing.total_interest(principal=amount, monthly_payment=monthly, term_months=term_months)

        return JsonResponse({
            "amount": _money(amount),
            "term_months": term_months,
            "commission_percent": _money(commission_percent),
            "lender": band.organisation.name,
            "monthly_payment": _money(monthly),
            "apr": _money(apr_value),
            "flat_rate": _money(flat),
            "total_interest": _money(interest),
        })


def _bad_request(error: str, detail: str) -> JsonResponse:
    return JsonResponse({"error": error, "detail": detail}, status=400)


def _parse_decimal(payload: dict, field: str, *, positive: bool,
                   required: bool = True, default: Decimal | None = None):
    """Pull `field` from `payload` as a Decimal. Returns ``(value, error_response)``;
    exactly one is non-None."""
    if field not in payload:
        if required:
            return None, _bad_request("missing_field", f"`{field}` is required.")
        return default, None
    raw = payload[field]
    if raw is None and not required:
        return default, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, _bad_request("invalid_field", f"`{field}` must be a number.")
    if positive and value <= 0:
        return None, _bad_request("invalid_field", f"`{field}` must be greater than 0.")
    return value, None


def _parse_int(payload: dict, field: str, *, positive: bool):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.")
    raw = payload[field]
    # Reject floats and bools (bool is a subclass of int in Python — easy footgun).
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, _bad_request("invalid_field", f"`{field}` must be a whole number.")
    if positive and raw <= 0:
        return None, _bad_request("invalid_field", f"`{field}` must be greater than 0.")
    return raw, None
