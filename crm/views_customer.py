"""Customer portal views — the /my/ section.

A logged-in customer gets the same app shell as staff with a three-item nav
(Company / People / Deals) and inline HTMX editing: each record renders as a
card partial whose Edit button swaps in a form partial in place; saving swaps
the card back. Everything is scoped through request.user — foreign records
404. The wizard portal (crm.views Portal*) is untouched by this module.

The HTMX contract, used by every section:
- GET  …/<pk>/card/  -> display partial (Cancel's target)
- GET  …/<pk>/edit/  -> form partial
- POST …/<pk>/edit/  -> valid: display partial; invalid: form partial w/ errors
Non-HTMX requests (no HX-Request header) degrade to full-page redirects.
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from .forms import CustomerCompanyForm, CustomerInfoForm
from .models import Contact, Deal, Note, Organisation
from .views import CustomerRequiredMixin


def _log_customer_update(user, section: str, form, *, contact=None, organisation=None) -> None:
    """Audit trail: one Note per saved edit that actually changed something.

    Surfaces in the staff notes streams so staff can see what customers
    changed and when."""
    if not form.changed_data:
        return
    labels = [str(form.fields[name].label or name) for name in form.changed_data]
    Note.objects.create(
        type=Note.Type.CUSTOMER_UPDATE,
        author=user,
        datetime=timezone.now(),
        content=f"Customer updated {section}: {', '.join(labels)}.",
        contact=contact,
        organisation=organisation,
    )


def customer_organisations(user):
    """Orgs a customer may see/edit: those on their own deals plus those
    their contact belongs to — the same effective grant as the wizard."""
    return (
        Organisation.objects.filter(
            Q(deals__customer__user=user) | Q(contacts__user=user)
        )
        .distinct()
        .order_by("name")
    )


def customer_people(user):
    """People a customer may see/edit: themselves plus co-applicants on
    their deals. Never other contacts of their organisations. Tolerates a
    user with no linked contact (dup-email edge) — they just see less."""
    return (
        Contact.objects.filter(
            Q(user=user) | Q(co_applicant_deals__customer__user=user)
        )
        .distinct()
        .order_by("last_name", "first_name")
    )


def customer_deals(user):
    """Same scoping rule as the wizard's _PortalDealMixin."""
    return (
        Deal.objects.filter(customer__user=user)
        .select_related("organisation", "selected_quote")
        .order_by("-created_at")
    )


class _CustomerCardView(CustomerRequiredMixin, View):
    """Base for one inline-editable section: card + edit endpoints.

    Subclasses set `form_class`, `section` (audit label), `object_name`
    (context key), `card_template`, `form_template`, `page_url` and implement
    `get_queryset()` / `note_kwargs(obj)`."""

    form_class = None
    section = ""
    object_name = "object"
    card_template = ""
    form_template = ""
    page_url = ""

    def get_queryset(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def get_object(self, pk):
        return get_object_or_404(self.get_queryset(), pk=pk)

    def is_htmx(self, request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def prefix(self, obj) -> str:
        # Per-object prefix so two open edit forms never collide on field ids.
        return f"{self.object_name}{obj.pk}"

    def render_card(self, request, obj):
        return render(request, self.card_template, {self.object_name: obj})

    def render_form(self, request, obj, form):
        return render(request, self.form_template, {self.object_name: obj, "form": form})

    def note_kwargs(self, obj) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


class _CardPartialView(_CustomerCardView):
    """GET …/<pk>/card/ — the display partial (Cancel lands here)."""

    def get(self, request, pk):
        obj = self.get_object(pk)
        if not self.is_htmx(request):
            return redirect(self.page_url)
        return self.render_card(request, obj)


class _EditPartialView(_CustomerCardView):
    """GET …/<pk>/edit/ — form partial. POST — save, return card partial."""

    def get(self, request, pk):
        obj = self.get_object(pk)
        if not self.is_htmx(request):
            return redirect(self.page_url)
        form = self.form_class(instance=obj, prefix=self.prefix(obj))
        return self.render_form(request, obj, form)

    def post(self, request, pk):
        obj = self.get_object(pk)
        form = self.form_class(request.POST, instance=obj, prefix=self.prefix(obj))
        if form.is_valid():
            form.save()
            _log_customer_update(request.user, self.section, form, **self.note_kwargs(obj))
            if not self.is_htmx(request):
                messages.success(request, f"{self.section.capitalize()} saved.")
                return redirect(self.page_url)
            return self.render_card(request, obj)
        if not self.is_htmx(request):
            messages.error(request, f"Couldn't save {self.section} — please try again.")
            return redirect(self.page_url)
        return self.render_form(request, obj, form)


# --- Company ---------------------------------------------------------------

class _CompanyMixin:
    form_class = CustomerCompanyForm
    section = "company details"
    object_name = "org"
    card_template = "crm/_my_company_card.html"
    form_template = "crm/_my_company_form.html"
    page_url = "crm:my_company"

    def get_queryset(self):
        return customer_organisations(self.request.user)

    def note_kwargs(self, obj):
        return {"organisation": obj}


class MyCompanyView(CustomerRequiredMixin, View):
    def get(self, request):
        return render(
            request,
            "crm/my_company.html",
            {"organisations": customer_organisations(request.user)},
        )


class MyCompanyCardView(_CompanyMixin, _CardPartialView):
    pass


class MyCompanyEditView(_CompanyMixin, _EditPartialView):
    pass


# --- People ----------------------------------------------------------------

class _PersonMixin:
    form_class = CustomerInfoForm
    section = "personal details"
    object_name = "person"
    card_template = "crm/_my_person_card.html"
    form_template = "crm/_my_person_form.html"
    page_url = "crm:my_people"

    def get_queryset(self):
        return customer_people(self.request.user)

    def note_kwargs(self, obj):
        return {"contact": obj}


class MyPeopleView(CustomerRequiredMixin, View):
    def get(self, request):
        return render(
            request,
            "crm/my_people.html",
            {"people": customer_people(request.user)},
        )


class MyPersonCardView(_PersonMixin, _CardPartialView):
    pass


class MyPersonEditView(_PersonMixin, _EditPartialView):
    pass


# --- Deals (read-only this slice) ------------------------------------------

class MyDealListView(CustomerRequiredMixin, View):
    def get(self, request):
        return render(
            request,
            "crm/my_deal_list.html",
            {"deals": customer_deals(request.user)},
        )


class MyDealDetailView(CustomerRequiredMixin, View):
    def get(self, request, pk):
        deal = get_object_or_404(
            customer_deals(request.user).select_related("owner"), pk=pk
        )
        amount = deal.finance_amount
        return render(
            request,
            "crm/my_deal_detail.html",
            {
                "deal": deal,
                "amount_display": f"£{amount:,.2f}" if amount is not None else None,
                "documents": deal.documents.all(),
                "repayment_schedule": deal.repayment_schedule if deal.first_payment_date else None,
            },
        )
