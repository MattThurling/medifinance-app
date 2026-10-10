"""JSON APIs.

Two surfaces live here:

* **Internal extension API** (`StaffApiView` + `DealListApi` / `DealDetailApi`):
  read-only, session-cookie auth via the staff member's CRM login. Consumed by
  the Medifinance browser extension to flat-fill partner application forms.

* **Public integrator API** (`BearerApiView` + `QuoteApi` / `DealCreateApi` /
  `ApplicationStatusApi`): stateless, `Authorization: Bearer <api_key>` auth
  (see `accounts.models.ApiKey`). Quotes an amount + term, creates a deal
  introduced by the key's organisation, or reports an application's status.
  Documented for partners in `docs/partner-api.md`.

Everything here returns JSON 401/403/4xx rather than HTML redirects, so the
client can show a sensible message instead of trying to parse a login page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
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
from .access import visible_deals
from .models import (
    Contact, Deal, Document, Participation, PartnerApiRequest, Quote, RateBand, Stage,
)

# Per-token rate limits for POST /api/deals/. Counts deals created by this
# ApiKey in the rolling window — dedup'd repeats don't count, so a partner
# safely retrying the same payload never trips the limit. Configured via the
# API_DEAL_RATE_LIMIT_HOUR/DAY env vars (5/25 defaults; high on dev for demos).
RATE_LIMIT_HOUR_MAX = settings.API_DEAL_RATE_LIMIT_HOUR
RATE_LIMIT_DAY_MAX = settings.API_DEAL_RATE_LIMIT_DAY

# Window during which a repeat (introducer + customer email) returns the
# existing deal instead of creating a new one. Configured via the
# API_DEAL_DEDUP_SECONDS env var (24h default; 10s on dev for demos).
DEDUP_WINDOW = timedelta(seconds=settings.API_DEAL_DEDUP_SECONDS)

# Per-applicant-email limit on NEW deals across every partner key (per hour).
EMAIL_RATE_LIMIT_HOUR_MAX = settings.API_DEAL_EMAIL_RATE_LIMIT_HOUR

# Amount bounds a partner may submit; outside -> 422.
MIN_AMOUNT = Decimal(settings.API_DEAL_MIN_AMOUNT)
MAX_AMOUNT = Decimal(settings.API_DEAL_MAX_AMOUNT)

# How long a stored successful response is replayed for a repeat Idempotency-Key.
IDEMPOTENCY_WINDOW = timedelta(seconds=settings.API_IDEMPOTENCY_SECONDS)
IDEMPOTENCY_KEY_MAX_LENGTH = 255

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
        "customer": (customer.full_name or customer.email) if customer else None,
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
            visible_deals(request.user)
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
                visible_deals(request.user)
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
            return _bad_request("invalid_field", "commission_percent must not be negative.", "commission_percent")

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

CHANNEL_VALUES = [c.value for c in Deal.Channel]
COMPANY_TYPE_VALUES = [c.value for c in Deal.CompanyType]

# Customer-facing wording returned to partners in `message`. Partner sites and
# AI agents show it verbatim, so it is deliberately factual: no finance terms,
# no rates, no promotion. Any change here needs Medifinance sign-off.
MSG_LINK_SENT = (
    "We've emailed {email} a secure link to complete the finance application. "
    "Nothing is agreed or charged at this stage."
)
MSG_EXISTING = (
    "We already have a recent application for {email}, so we haven't started "
    "another one. The secure link to complete it was emailed earlier — please "
    "check your inbox (and spam folder)."
)
MSG_SANDBOX = (
    "Sandbox: the application passed validation. No application was created "
    "and no email was sent."
)

# Application `status` values. The create step only ever yields the first:
# the customer must still complete the regulated steps via their secure link.
STATUS_AWAITING_CUSTOMER = "awaiting_customer"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"
STATUS_SANDBOX = "sandbox"

SANDBOX_REFERENCE_PREFIX = "MF-SANDBOX-"


def application_reference(deal_pk: int) -> str:
    """The stable public reference for a deal (matches the extension payload)."""
    return f"MF-{deal_pk}"


def _request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DealCreateApi(BearerApiView):
    """POST /api/deals/  -> create a deal introduced by the key's organisation.

    Request body (JSON). Required fields are unchanged from v1; everything
    else is optional and additive:
        {
          "name": "Dental chair refit",      // or "description"
          "amount": 25000,
          "first_name": "Jane",
          "last_name": "Smith",
          "email": "jane@example.com",
          "ltd": true,                       // or "company_type"
          "company_type": "limited_company", // limited_company|sole_trader|partnership|other
          "channel": "web-guest",            // web|web-guest|mcp|api
          "agent_client": "claude",          // free text, informational
          "product_id": "chair-package",     // partner's own SKU
          "asset_category": "surgery"
        }
    Optional header: `Idempotency-Key: <unique string>` — a repeat within the
    idempotency window with the same key AND body returns the original
    successful response; the same key with a different body is a 409.

    Side effects: the customer Contact is reused (matched by email) or created;
    the key's organisation is set as both the deal's introducer and the
    participation's supplier; a quote is attached for every term that has an
    active rate band covering the amount (the cheapest lender per term, each at
    a default 3% commission); the standard document requests are added; the
    NOTIFY_EMAILS staff list is emailed; and the customer is sent a portal
    magic link — the same one staff would send by pressing "Send Application…".
    Every attempt, whatever the outcome, is logged to `PartnerApiRequest`.

    Safeguards against customer spam if a partner token is compromised:

    * Global kill switch: `accounts.SiteSettings.api_enabled` (toggleable from
      the dashboard) blocks every POST with 503 when off.
    * Per-customer dedup: a repeat (introducer + email) within the dedup window
      returns the existing deal — no new quotes, no second customer email.
    * Per-token rate limit: 5/hour and 25/day of *new* (non-dedup'd) deals per
      key returns 429.
    * Per-email rate limit: 3/hour of new deals for one applicant email across
      all keys returns 429.
    * Sandbox keys (`ApiKey.is_sandbox`) validate and return a fake reference
      without creating anything or sending anything.

    Response (201):
        {"id": 42, "name": "Dental chair refit", "amount": "25000.00",
         "deduplicated": false, "application_reference": "MF-42",
         "status": "awaiting_customer", "message": "We've emailed …",
         "sandbox": false}

    A dedup'd call returns 200 with `"deduplicated": true` and the existing
    deal's id/reference.

    Errors (always JSON, with a flat `error`/`detail` and a structured
    `errors` list):
        400 — malformed body / missing or invalid field
        401 — bad/missing bearer token
        409 — Idempotency-Key reused with a different body
        422 — amount outside the configured bounds
        429 — rate limit exceeded (per key or per applicant email)
        503 — API access is currently disabled site-wide
    """

    def post(self, request):
        self.idempotency_key = (request.META.get("HTTP_IDEMPOTENCY_KEY") or "").strip()
        self.meta = {"channel": "", "agent_client": "", "email": "", "request_hash": ""}

        if not SiteSettings.get().api_enabled:
            return self._finish(
                JsonResponse(
                    {"error": "api_disabled",
                     "detail": "Partner API access is currently disabled. "
                               "Contact Medifinance to restore service.",
                     "errors": [{"field": None, "code": "api_disabled",
                                 "message": "Partner API access is currently disabled. "
                                            "Contact Medifinance to restore service."}]},
                    status=503,
                ),
                PartnerApiRequest.Outcome.DISABLED,
            )

        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return self._reject(_bad_request("invalid_json", "Request body must be valid JSON."))
        if not isinstance(payload, dict):
            return self._reject(_bad_request("invalid_json", "Request body must be a JSON object."))

        # Record what we can about the attempt before validating, so even a
        # rejected submission is attributable in the audit log.
        self.meta["request_hash"] = _request_hash(payload)
        for key in ("channel", "agent_client", "email"):
            raw = payload.get(key)
            if isinstance(raw, str):
                self.meta[key] = raw.strip()[:254]

        if len(self.idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            return self._reject(_bad_request(
                "invalid_field",
                f"`Idempotency-Key` must be at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters.",
                field="Idempotency-Key",
            ))

        # Idempotent replay: same key + same body within the window returns
        # the original successful response verbatim. Only successes are
        # replayed — a previous 4xx/5xx should be re-attempted afresh.
        if self.idempotency_key:
            original = (
                PartnerApiRequest.objects
                .filter(
                    api_key=request.api_key,
                    idempotency_key=self.idempotency_key,
                    outcome__in=(
                        PartnerApiRequest.Outcome.CREATED,
                        PartnerApiRequest.Outcome.DEDUPLICATED,
                        PartnerApiRequest.Outcome.SANDBOX,
                    ),
                    created_at__gte=timezone.now() - IDEMPOTENCY_WINDOW,
                )
                .order_by("created_at")
                .first()
            )
            if original is not None:
                if original.request_hash != self.meta["request_hash"]:
                    return self._finish(
                        _error_response(
                            "idempotency_key_reused",
                            "This Idempotency-Key was already used with a different "
                            "request body. Use a new key for a new application.",
                            status=409, field="Idempotency-Key", code="reused",
                        ),
                        PartnerApiRequest.Outcome.CONFLICT,
                    )
                return self._finish(
                    JsonResponse(original.response_body, status=original.status_code),
                    PartnerApiRequest.Outcome.REPLAYED,
                    deal=original.deal, replay_of=original,
                )

        # --- validation ---------------------------------------------------
        # `name` is the v1 field; `description` is accepted as a synonym so
        # agent-facing partners can send the asset description naturally.
        if "name" in payload or "description" not in payload:
            name, err = _parse_str(payload, "name", max_length=255)
        else:
            name, err = _parse_str(payload, "description", max_length=255)
        if err:
            return self._reject(err)
        amount, err = _parse_decimal(payload, "amount", positive=True)
        if err:
            return self._reject(err)
        if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
            return self._reject(_error_response(
                "amount_out_of_range",
                f"`amount` must be between £{MIN_AMOUNT:,.0f} and £{MAX_AMOUNT:,.0f}.",
                status=422, field="amount", code="out_of_range",
            ))
        first_name, err = _parse_str(payload, "first_name", max_length=150)
        if err:
            return self._reject(err)
        last_name, err = _parse_str(payload, "last_name", max_length=150)
        if err:
            return self._reject(err)
        email, err = _parse_str(payload, "email", max_length=254)
        if err:
            return self._reject(err)
        try:
            validate_email(email)
        except ValidationError:
            return self._reject(_bad_request(
                "invalid_field", "`email` must be a valid email address.", field="email",
            ))
        self.meta["email"] = email

        company_type, err = _parse_choice(
            payload, "company_type", choices=COMPANY_TYPE_VALUES, required=False,
        )
        if err:
            return self._reject(err)
        if "ltd" in payload or company_type is None:
            # v1 contract: `ltd` is required unless `company_type` says it all.
            ltd, err = _parse_bool(payload, "ltd")
            if err:
                return self._reject(err)
        else:
            ltd = company_type == Deal.CompanyType.LIMITED_COMPANY
        if company_type is None and ltd:
            company_type = Deal.CompanyType.LIMITED_COMPANY

        channel, err = _parse_choice(payload, "channel", choices=CHANNEL_VALUES, required=False)
        if err:
            return self._reject(err)
        agent_client, err = _parse_optional_str(payload, "agent_client", max_length=64)
        if err:
            return self._reject(err)
        product_id, err = _parse_optional_str(payload, "product_id", max_length=100)
        if err:
            return self._reject(err)
        asset_category, err = _parse_optional_str(payload, "asset_category", max_length=100)
        if err:
            return self._reject(err)
        self.meta["channel"] = channel or ""
        self.meta["agent_client"] = agent_client

        introducer = request.api_key.organisation

        # --- sandbox ------------------------------------------------------
        if request.api_key.is_sandbox:
            return self._finish(
                JsonResponse(
                    {
                        "id": None,
                        "name": name,
                        "amount": _money(amount),
                        "deduplicated": False,
                        "application_reference":
                            SANDBOX_REFERENCE_PREFIX + secrets.token_hex(4).upper(),
                        "status": STATUS_SANDBOX,
                        "message": MSG_SANDBOX,
                        "sandbox": True,
                    },
                    status=201,
                ),
                PartnerApiRequest.Outcome.SANDBOX,
            )

        # --- dedup --------------------------------------------------------
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
            return self._finish(
                JsonResponse(
                    {
                        "id": existing.pk,
                        "name": existing.name,
                        "amount": _money(existing_amount) if existing_amount is not None else None,
                        "deduplicated": True,
                        "application_reference": application_reference(existing.pk),
                        "status": STATUS_AWAITING_CUSTOMER,
                        "message": MSG_EXISTING.format(email=email),
                        "sandbox": False,
                    },
                    status=200,
                ),
                PartnerApiRequest.Outcome.DEDUPLICATED,
                deal=existing,
            )

        # --- rate limits --------------------------------------------------
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
            return self._finish(
                _error_response(
                    "rate_limited",
                    f"Too many deals from this key. Limit is "
                    f"{RATE_LIMIT_HOUR_MAX}/hour and {RATE_LIMIT_DAY_MAX}/day. "
                    f"Try again later.",
                    status=429, field=None, code="rate_limited",
                ),
                PartnerApiRequest.Outcome.RATE_LIMITED,
            )

        # Per-applicant-email limit across ALL keys — stops a compromised or
        # over-eager integration flooding one inbox with magic links.
        email_count = (
            Deal.objects
            .filter(
                created_via_api_key__isnull=False,
                customer__email__iexact=email,
                created_at__gte=now - timedelta(hours=1),
            )
            .count()
        )
        if email_count >= EMAIL_RATE_LIMIT_HOUR_MAX:
            logger.warning(
                "Per-email rate limit hit for %s via ApiKey %s: %d/h",
                email, request.api_key.prefix, email_count,
            )
            return self._finish(
                _error_response(
                    "email_rate_limited",
                    f"Too many applications for {email} in the last hour "
                    f"(limit {EMAIL_RATE_LIMIT_HOUR_MAX}). Ask the applicant to "
                    f"check their inbox for the link already sent, or try again later.",
                    status=429, field="email", code="rate_limited",
                ),
                PartnerApiRequest.Outcome.RATE_LIMITED,
            )

        # --- create -------------------------------------------------------
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
                channel=channel or "",
                agent_client=agent_client,
                partner_product_id=product_id,
                asset_category=asset_category,
                company_type=company_type or "",
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
                via_partner=introducer.name,
            )
        except NoCustomerEmailError:
            # Email was validated above, so this shouldn't fire — log loudly if it does.
            logger.exception("Portal link skipped (no email) for API deal %s", deal.pk)
        except Exception:
            logger.exception("Portal link email failed for API deal %s", deal.pk)

        return self._finish(
            JsonResponse(
                {
                    "id": deal.pk,
                    "name": deal.name,
                    "amount": _money(amount),
                    "deduplicated": False,
                    "application_reference": application_reference(deal.pk),
                    "status": STATUS_AWAITING_CUSTOMER,
                    "message": MSG_LINK_SENT.format(email=email),
                    "sandbox": False,
                },
                status=201,
            ),
            PartnerApiRequest.Outcome.CREATED,
            deal=deal,
        )

    # --- audit log ---------------------------------------------------------

    def _reject(self, response: JsonResponse) -> JsonResponse:
        return self._finish(response, PartnerApiRequest.Outcome.REJECTED)

    def _finish(self, response: JsonResponse, outcome: str, *, deal=None,
                replay_of=None) -> JsonResponse:
        """Log the attempt and return the response. Logging must never break
        the response the partner gets, so failures are swallowed and logged."""
        try:
            body = json.loads(response.content)
        except ValueError:
            body = None
        try:
            PartnerApiRequest.objects.create(
                api_key=self.request.api_key,
                organisation=self.request.api_key.organisation,
                idempotency_key=self.idempotency_key[:IDEMPOTENCY_KEY_MAX_LENGTH],
                request_hash=self.meta["request_hash"],
                channel=self.meta["channel"][:32],
                agent_client=self.meta["agent_client"][:64],
                email=self.meta["email"][:254],
                outcome=outcome,
                status_code=response.status_code,
                response_body=body,
                deal=deal,
                replay_of=replay_of,
            )
        except Exception:
            logger.exception("Failed to log partner API request (outcome=%s)", outcome)
        return response


class ApplicationStatusApi(BearerApiView):
    """GET /api/applications/<reference>/status/  -> coarse status of one
    application this partner introduced. `reference` is the
    `application_reference` returned on create (e.g. `MF-42`).

    Response (200):
        {"application_reference": "MF-42", "status": "awaiting_customer",
         "stage": null, "name": "…", "amount": "25000.00",
         "channel": "web-guest", "created_at": "2026-10-10T11:02:03+00:00"}

    `status` is one of `awaiting_customer` (the applicant hasn't completed
    their secure link yet), `in_progress` (Medifinance is working the deal),
    `closed` (lost, dormant or settled) — or `sandbox` for sandbox references.
    It never says whether credit has been approved; that's communicated to
    the customer by Medifinance directly.

    Errors: 401 bad token; 404 unknown reference or not this partner's.
    """

    def get(self, request, reference: str):
        reference = reference.strip().upper()
        if reference.startswith(SANDBOX_REFERENCE_PREFIX):
            return JsonResponse({
                "application_reference": reference,
                "status": STATUS_SANDBOX,
                "stage": None,
                "sandbox": True,
            })
        pk = None
        if reference.startswith("MF-") and reference[3:].isdigit():
            pk = int(reference[3:])
        deal = None
        if pk is not None:
            deal = (
                Deal.objects
                .filter(pk=pk, introducer=request.api_key.organisation)
                .select_related("customer")
                .first()
            )
        if deal is None:
            return _error_response(
                "not_found",
                "No application with that reference was introduced by your organisation.",
                status=404, field="reference", code="not_found",
            )
        # Every deal is seeded with an "Application" stage on create (see
        # crm/signals.py), so that stage with no chosen quote means the
        # customer hasn't completed their secure link yet.
        stage = deal.current_stage
        stage_name = stage.name if stage else None
        if stage_name in (Stage.Name.LOST, Stage.Name.DORMANT, Stage.Name.SETTLED):
            status = STATUS_CLOSED
        elif stage_name in (None, Stage.Name.APPLICATION) and not deal.selected_quote_id:
            status = STATUS_AWAITING_CUSTOMER
        else:
            status = STATUS_IN_PROGRESS
        amount = deal.funded_amount
        return JsonResponse({
            "application_reference": application_reference(deal.pk),
            "status": status,
            "stage": stage_name,
            "name": deal.name,
            "amount": _money(amount) if amount is not None else None,
            "channel": deal.channel or None,
            "created_at": deal.created_at.isoformat(),
            "sandbox": False,
        })


@method_decorator(csrf_exempt, name="dispatch")
class DealRootApi(View):
    """/api/deals/ serves two auth schemes: GET is the staff extension list
    (session auth), POST is the public integrator create (bearer auth)."""

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            return DealCreateApi.as_view()(request, *args, **kwargs)
        return DealListApi.as_view()(request, *args, **kwargs)


def _error_response(error: str, detail: str, *, status: int, field: str | None,
                    code: str) -> JsonResponse:
    """JSON error. `error`/`detail` is the original flat shape (kept for v1
    integrators); `errors` is the structured list AI agents read to correct
    themselves — one entry per problem, naming the field where there is one."""
    return JsonResponse(
        {
            "error": error,
            "detail": detail,
            "errors": [{"field": field, "code": code, "message": detail}],
        },
        status=status,
    )


_BAD_REQUEST_CODES = {"missing_field": "required", "invalid_field": "invalid"}


def _bad_request(error: str, detail: str, field: str | None = None) -> JsonResponse:
    return _error_response(
        error, detail, status=400, field=field,
        code=_BAD_REQUEST_CODES.get(error, error),
    )


def _parse_decimal(payload: dict, field: str, *, positive: bool,
                   required: bool = True, default: Decimal | None = None):
    """Pull `field` from `payload` as a Decimal. Returns ``(value, error_response)``;
    exactly one is non-None."""
    if field not in payload:
        if required:
            return None, _bad_request("missing_field", f"`{field}` is required.", field)
        return default, None
    raw = payload[field]
    if raw is None and not required:
        return default, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, _bad_request("invalid_field", f"`{field}` must be a number.", field)
    if positive and value <= 0:
        return None, _bad_request("invalid_field", f"`{field}` must be greater than 0.", field)
    return value, None


def _parse_str(payload: dict, field: str, *, max_length: int):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.", field)
    raw = payload[field]
    if not isinstance(raw, str) or not raw.strip():
        return None, _bad_request("invalid_field", f"`{field}` must be a non-empty string.", field)
    value = raw.strip()
    if len(value) > max_length:
        return None, _bad_request(
            "invalid_field", f"`{field}` must be at most {max_length} characters.", field,
        )
    return value, None


def _parse_optional_str(payload: dict, field: str, *, max_length: int):
    """Optional string: absent / null / blank -> "" (never an error)."""
    raw = payload.get(field)
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, _bad_request("invalid_field", f"`{field}` must be a string.", field)
    value = raw.strip()
    if len(value) > max_length:
        return None, _bad_request(
            "invalid_field", f"`{field}` must be at most {max_length} characters.", field,
        )
    return value, None


def _parse_choice(payload: dict, field: str, *, choices: list[str], required: bool):
    """A string that must be one of `choices`. Optional fields return None when
    absent/null; the error message lists the allowed values so an agent can
    correct itself."""
    raw = payload.get(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            return None, _bad_request("missing_field", f"`{field}` is required.", field)
        return None, None
    if not isinstance(raw, str) or raw.strip() not in choices:
        return None, _bad_request(
            "invalid_field",
            f"`{field}` must be one of: {', '.join(choices)}.", field,
        )
    return raw.strip(), None


def _parse_bool(payload: dict, field: str):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.", field)
    raw = payload[field]
    if not isinstance(raw, bool):
        return None, _bad_request("invalid_field", f"`{field}` must be true or false.", field)
    return raw, None


def _parse_int(payload: dict, field: str, *, positive: bool):
    if field not in payload:
        return None, _bad_request("missing_field", f"`{field}` is required.", field)
    raw = payload[field]
    # Reject floats and bools (bool is a subclass of int in Python — easy footgun).
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, _bad_request("invalid_field", f"`{field}` must be a whole number.", field)
    if positive and raw <= 0:
        return None, _bad_request("invalid_field", f"`{field}` must be greater than 0.", field)
    return raw, None
