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
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from accounts.models import ApiKey, SiteSettings

from . import pricing
from .models import Contact, Deal, Document, Participation, Quote, RateBand

# Per-token rate limits for POST /api/deals/. Counts deals created by this
# ApiKey in the rolling window — dedup'd repeats don't count, so a partner
# safely retrying the same payload never trips the limit.
# Demo settings: limits loosened so repeated demo submissions create real deals.
RATE_LIMIT_HOUR_MAX = 500
RATE_LIMIT_DAY_MAX = 2500

# Window during which a repeat (introducer + customer email) returns the
# existing deal instead of creating a new one. Configured via the
# API_DEAL_DEDUP_SECONDS env var (24h default; 10s on dev for demos).
DEDUP_WINDOW = timedelta(seconds=settings.API_DEAL_DEDUP_SECONDS)

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
            "deposit": _money(quote.deposit),
            "balloon": _money(quote.balloon),
            "repayment_profile": quote.repayment_profile or None,
            "finance_amount": _money(quote.finance_amount),
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
    the key's organisation is set as both the deal's introducer and the
    participation's supplier; a quote is attached for every term that has an
    active rate band covering the amount (the cheapest lender per term, each at
    a default 3% commission); the standard document requests are added; the
    NOTIFY_EMAILS staff list is emailed; and the customer is sent a portal
    magic link — the same one staff would send by pressing "Send Application…".

    Safeguards against customer spam if a partner token is compromised:

    * Global kill switch: `accounts.SiteSettings.api_enabled` (toggleable from
      the dashboard) blocks every POST with 503 when off.
    * Per-customer dedup: a repeat (introducer + email) within the dedup window
      returns the existing deal — no new quotes, no second customer email.
    * Per-token rate limit: 5/hour and 25/day of *new* (non-dedup'd) deals per
      key returns 429.

    Response (201):
        {"id": 42, "name": "Dental chair refit", "amount": "25000.00",
         "deduplicated": false}

    A dedup'd call returns 200 with `"deduplicated": true` and the existing
    deal's id.

    Errors (always JSON):
        400 — malformed body / missing or invalid field
        401 — bad/missing bearer token
        429 — rate limit exceeded for this key
        503 — API access is currently disabled site-wide
    """

    def post(self, request):
        if not SiteSettings.get().api_enabled:
            return JsonResponse(
                {"error": "api_disabled",
                 "detail": "Partner API access is currently disabled. "
                           "Contact Medifinance to restore service."},
                status=503,
            )

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

        # Dedup BEFORE rate limit, so a partner safely retrying the same payload
        # never trips the limit. Match introducer + customer email within the
        # window; return the most recent existing deal.
        dedup_cutoff = timezone.now() - DEDUP_WINDOW
        existing = (
            Deal.objects
            .filter(
                introducer=introducer,
                customer__email__iexact=email,
                created_at__gte=dedup_cutoff,
            )
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            existing_amount = existing.funded_amount
            return JsonResponse(
                {
                    "id": existing.pk,
                    "name": existing.name,
                    "amount": _money(existing_amount) if existing_amount is not None else None,
                    "deduplicated": True,
                },
                status=200,
            )

        # Per-token rate limit on NEW deals created in the rolling windows.
        now = timezone.now()
        recent_qs = Deal.objects.filter(created_via_api_key=request.api_key)
        hour_count = recent_qs.filter(created_at__gte=now - timedelta(hours=1)).count()
        day_count = recent_qs.filter(created_at__gte=now - timedelta(days=1)).count()
        if hour_count >= RATE_LIMIT_HOUR_MAX or day_count >= RATE_LIMIT_DAY_MAX:
            logger.warning(
                "Rate limit hit for ApiKey %s (org=%s): %d/h, %d/d",
                request.api_key.prefix, introducer.name, hour_count, day_count,
            )
            return JsonResponse(
                {"error": "rate_limited",
                 "detail": f"Too many deals from this key. Limit is "
                           f"{RATE_LIMIT_HOUR_MAX}/hour and "
                           f"{RATE_LIMIT_DAY_MAX}/day."},
                status=429,
            )

        with transaction.atomic():
            contact = Contact.objects.filter(email__iexact=email).first()
            if contact is None:
                contact = Contact.objects.create(
                    first_name=first_name, last_name=last_name, email=email,
                )
            deal = Deal.objects.create(
                name=name,
                customer=contact,
                introducer=introducer,
                created_via_api_key=request.api_key,
            )
            Participation.objects.create(deal=deal, amount=amount, organisation=introducer)

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
                quotes.append(Quote.objects.create(
                    deal=deal, rate=band, term=term,
                    commission_percent=Decimal("3"),
                ))

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

        # Send the customer the same portal magic link the "Send Application…"
        # button would. Wrapped because a mailserver hiccup shouldn't 500 the
        # integrator on an already-committed deal.
        try:
            from .portal_links import issue_portal_link_for_deal, NoCustomerEmailError
            from accounts.emails import send_magic_link_email
            link, _dup_warning = issue_portal_link_for_deal(deal, created_by=None)
            full_url = request.build_absolute_uri(
                reverse("consume_magic_link", args=[link.token])
            )
            send_magic_link_email(
                to_email=link.user.email,
                link_url=full_url,
                deal_name=deal.name,
                owner_name="The team",
                expires_at=link.expires_at,
            )
        except NoCustomerEmailError:
            # Email was validated above, so this shouldn't fire — log loudly if it does.
            logger.exception("Portal link skipped (no email) for API deal %s", deal.pk)
        except Exception:
            logger.exception("Portal link email failed for API deal %s", deal.pk)

        return JsonResponse(
            {"id": deal.pk, "name": deal.name, "amount": _money(amount),
             "deduplicated": False},
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
