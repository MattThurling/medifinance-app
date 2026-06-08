"""Read-only JSON API consumed by the Medifinance browser extension.

These endpoints expose deal data in a flat, form-fill-friendly shape so the
extension can populate a partner/bank application form from a CRM deal.

Auth piggybacks on the staff member's existing CRM session cookie (the
extension is granted host access to the CRM, so the browser sends the cookie).
Everything here is GET-only, so Django's CSRF check doesn't apply. On an auth
failure we return JSON (401/403) rather than the HTML login redirect so the
extension can show a sensible message instead of trying to parse a login page.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Q
from django.http import JsonResponse
from django.views import View

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
