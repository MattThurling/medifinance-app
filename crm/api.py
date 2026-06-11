"""JSON APIs.

Two surfaces live here:

* **Internal extension API** (`StaffApiView` + `DealListApi` / `DealDetailApi`):
  read-only, session-cookie auth via the staff member's CRM login. Consumed by
  the Medifinance browser extension to flat-fill partner application forms.

* **Public integrator API** (`BearerApiView` + `QuoteApi` / `DealCreateApi`):
  stateless POST, `Authorization: Bearer <api_key>` auth (see
  `accounts.models.ApiKey`). Quotes an amount + term, or creates a deal
  introduced by the key's organisation.

Everything here returns JSON 401/403/4xx rather than HTML redirects, so the
client can show a sensible message instead of trying to parse a login page.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from accounts.models import ApiKey

from . import pricing
from .models import Contact, Deal, Document, Participation, Quote, RateBand

logger = logging.getLogger(__name__)


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
        } if deal.owner else None,
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


DEFAULT_DOCUMENT_REQUESTS = [
    "Last 3 months business bank statements",
    "Most recent financial accounts or tax returns",
]


class DealCreateApi(BearerApiView):
    """POST /api/deals/  -> create a deal introduced by the key's organisation.

    Request body (JSON):
        {
          "name": "Dental chair refit",
          "amount": 25000,
          "first_name": "Jane",
          "last_name": "Smith",
          "email": "jane@example.com",
          "ltd": true                 // is the business a limited company?
        }

    Side effects: the customer Contact is reused (matched by email) or created;
    a quote is attached for every term that has an active rate band covering
    the amount (the cheapest lender per term); the standard document requests
    are added; and the NOTIFY_EMAILS staff list is emailed.

    Response (201):
        {"id": 42, "name": "Dental chair refit", "amount": "25000.00"}

    Errors (always JSON):
        400 — malformed body / missing or invalid field
        401 — bad/missing bearer token
    """

    def post(self, request):
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return _bad_request("invalid_json", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            return _bad_request("invalid_json", "Request body must be a JSON object.")

        name, err = _parse_str(payload, "name", max_length=255)
        if err:
            return err
        amount, err = _parse_decimal(payload, "amount", positive=True)
        if err:
            return err
        first_name, err = _parse_str(payload, "first_name", max_length=150)
        if err:
            return err
        last_name, err = _parse_str(payload, "last_name", max_length=150)
        if err:
            return err
        email, err = _parse_str(payload, "email", max_length=254)
        if err:
            return err
        try:
            validate_email(email)
        except ValidationError:
            return _bad_request("invalid_field", "`email` must be a valid email address.")
        ltd, err = _parse_bool(payload, "ltd")
        if err:
            return err

        introducer = request.api_key.organisation

        with transaction.atomic():
            contact = Contact.objects.filter(email__iexact=email).first()
            if contact is None:
                contact = Contact.objects.create(
                    first_name=first_name, last_name=last_name, email=email,
                )
            deal = Deal.objects.create(name=name, customer=contact, introducer=introducer)
            Participation.objects.create(deal=deal, amount=amount)

            quotes = []
            terms = (
                RateBand.objects.active()
                .filter(min_amount__lte=amount, max_amount__gte=amount)
                .order_by("term_months")
                .values_list("term_months", flat=True)
                .distinct()
            )
            for term in terms:
                band = pricing.cheapest_rate_band(term_months=term, amount=amount)
                quotes.append(Quote.objects.create(deal=deal, rate=band, term=term))

            for doc_name in DEFAULT_DOCUMENT_REQUESTS:
                Document.objects.create(deal=deal, name=doc_name)

        try:
            from accounts.emails import send_new_deal_notification_email
            send_new_deal_notification_email(
                deal_name=deal.name,
                deal_url=request.build_absolute_uri(deal.get_absolute_url()),
                introducer_name=introducer.name,
                customer_name=contact.full_name,
                customer_email=contact.email,
                amount_display=f"£{amount:,.2f}",
                is_limited_company=ltd,
                quote_count=len(quotes),
            )
        except Exception:
            # The deal is already committed — a broken mailserver shouldn't
            # turn a successful create into a 500 for the integrator.
            logger.exception("New-deal notification email failed for deal %s", deal.pk)

        return JsonResponse(
            {"id": deal.pk, "name": deal.name, "amount": _money(amount)},
            status=201,
        )


@method_decorator(csrf_exempt, name="dispatch")
class DealRootApi(View):
    """/api/deals/ serves two auth schemes: GET is the staff extension list
    (session auth), POST is the public integrator create (bearer auth)."""

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            return DealCreateApi.as_view()(request, *args, **kwargs)
        return DealListApi.as_view()(request, *args, **kwargs)


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


def _parse_str(payload: dict, field: str, *, max_length: int):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.")
    raw = payload[field]
    if not isinstance(raw, str) or not raw.strip():
        return None, _bad_request("invalid_field", f"`{field}` must be a non-empty string.")
    value = raw.strip()
    if len(value) > max_length:
        return None, _bad_request(
            "invalid_field", f"`{field}` must be at most {max_length} characters.",
        )
    return value, None


def _parse_bool(payload: dict, field: str):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.")
    raw = payload[field]
    if not isinstance(raw, bool):
        return None, _bad_request("invalid_field", f"`{field}` must be true or false.")
    return raw, None


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
