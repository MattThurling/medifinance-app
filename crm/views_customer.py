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

from django.conf import settings
from django.contrib import messages
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from . import docuseal
from .forms import CustomerCompanyForm, CustomerInfoForm, QuoteSelectionForm
from .models import Contact, Deal, Document, Note, Organisation, Quote, SignatureRequest
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
    """Same scoping rule as the wizard's _PortalDealMixin.

    Annotates each deal with its outstanding customer actions — documents
    still to provide and signature requests still to sign — so lists and the
    dashboard can badge "N actions needed" without N+1 queries.
    `distinct=True` on both Counts: two reverse joins would otherwise
    multiply each other's rows."""
    return (
        Deal.objects.filter(customer__user=user)
        .select_related("organisation", "selected_quote")
        .annotate(
            outstanding_docs=Count(
                "documents",
                filter=Q(documents__status=Document.Status.REQUESTED),
                distinct=True,
            ),
            outstanding_sigs=Count(
                "signature_requests",
                filter=Q(signature_requests__status__in=[
                    SignatureRequest.Status.SENT, SignatureRequest.Status.OPENED,
                ]),
                distinct=True,
            ),
            # 1 when quotes exist but none is chosen yet — badge-summable
            # alongside the two counts above.
            needs_quote=Case(
                When(
                    Q(selected_quote__isnull=True)
                    & Exists(Quote.objects.filter(deal=OuterRef("pk"))),
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
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
                "quotes": deal.quotes.all(),
                "signature_requests": deal.signature_requests.all(),
                "docuseal_ready": docuseal.is_configured(),
                "repayment_schedule": deal.repayment_schedule if deal.first_payment_date else None,
            },
        )


class MyDealQuoteSelectView(CustomerRequiredMixin, View):
    """Customer picks (or changes) their quote — the deal page's Quotes card
    posts here. HTMX gets the refreshed card partial back; non-HTMX degrades
    to a redirect. Every change logs a customer_update Note on the deal."""

    def post(self, request, pk):
        deal = get_object_or_404(customer_deals(request.user), pk=pk)
        form = QuoteSelectionForm(request.POST, deal=deal)
        is_htmx = request.headers.get("HX-Request") == "true"
        saved = False
        if form.is_valid():
            quote = form.cleaned_data["quote"]
            changed = deal.selected_quote_id != quote.pk
            deal.selected_quote = quote
            deal.save(update_fields=["selected_quote"])
            saved = True
            if changed:
                monthly = quote.monthly_payment
                detail = f"{quote.term} months"
                if monthly is not None:
                    detail += f" — £{monthly:,.2f}/mo"
                Note.objects.create(
                    type=Note.Type.CUSTOMER_UPDATE,
                    author=request.user,
                    datetime=timezone.now(),
                    content=f"Customer selected a quote: {detail}.",
                    deal=deal,
                )
        if is_htmx:
            return render(
                request,
                "crm/_my_quotes_card.html",
                {"deal": deal, "quotes": deal.quotes.all(), "quote_saved": saved,
                 "quote_error": None if saved else "Please pick one of the quotes."},
            )
        if saved:
            messages.success(request, "Quote selected — thank you.")
        else:
            messages.error(request, "Please pick one of the quotes.")
        return redirect("crm:my_deal_detail", pk=deal.pk)


class MyDealSignView(CustomerRequiredMixin, View):
    """Send the customer to DocuSeal's signing page for one of their deal's
    signature requests ({DOCUSEAL_URL}/s/{slug}, opened in a new tab from the
    deal page). A redirect rather than an embed — DocuSeal's embedded form
    component needs a Pro subscription on self-hosted instances. This view
    still owns the guards and the lazy slug backfill, so the portal URL is
    stable whatever DocuSeal plan is behind it.

    Deliberate trade-off: the deal's customer can open ANY active request on
    their deal, including one addressed to a co-applicant — in a small
    brokerage the customer often forwards or sits next to the co-signer, and
    DocuSeal's own audit log records who actually signed, from where. Staff
    can void/re-send if the wrong person signs."""

    def get(self, request, deal_pk, sr_pk):
        deal = get_object_or_404(customer_deals(request.user), pk=deal_pk)
        sr = get_object_or_404(deal.signature_requests, pk=sr_pk)

        if not docuseal.is_configured():
            messages.error(request, "Signing isn't available right now — please try again later.")
            return redirect("crm:my_deal_detail", pk=deal.pk)
        if not sr.is_active:
            messages.info(request, "That signature request is no longer awaiting a signature.")
            return redirect("crm:my_deal_detail", pk=deal.pk)

        if not sr.signing_slug:
            # Requests created before the slug was stored: backfill from DocuSeal.
            try:
                submission = docuseal.get_submission(sr.submission_id)
                submitters = submission.get("submitters") or []
                match = next(
                    (sub for sub in submitters if sub.get("id") == sr.submitter_id),
                    submitters[0] if submitters else None,
                )
                sr.signing_slug = (match or {}).get("slug") or ""
                if sr.signing_slug:
                    sr.save(update_fields=["signing_slug", "updated_at"])
            except docuseal.DocuSealError:
                sr.signing_slug = ""
            if not sr.signing_slug:
                messages.error(request, "Couldn't open the signing page — please try again later.")
                return redirect("crm:my_deal_detail", pk=deal.pk)

        return redirect(f"{settings.DOCUSEAL_URL.rstrip('/')}/s/{sr.signing_slug}")
