"""Customer portal views — the /my/ section.

A logged-in applicant's home is their deal page: a "what we still need from
you" summary (see crm/portal_checklist.py), inline-editable cards for each
applicant and the company, then quotes, documents, signatures and
repayments. `/` routes them there (or to the deals list when they have
several). Everything is scoped through request.user — foreign records 404.

Two roles on a deal (crm.access.deal_role):
- the **primary** applicant (`deal.customer`) edits every person and the
  company, chooses the quote, uploads, signs requests addressed to them, and
  adds or removes people from their company;
- a **co-applicant** sees the deal, edits only their own details, uploads,
  and signs only requests addressed to them. Other people's cards show
  names only.

The HTMX contract for the person/company cards:
- GET  …/<pk>/card/  -> display partial (Cancel's target)
- GET  …/<pk>/edit/  -> form partial
- POST …/<pk>/edit/  -> valid: display partial; invalid: form partial w/ errors
Non-HTMX requests (no HX-Request header) degrade to full-page redirects.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from .. import applicants, docuseal
from ..access import PRIMARY, deal_role, may_sign, portal_deals
from ..forms import (
    CustomerApplicantForm,
    CustomerCompanyForm,
    CustomerInfoForm,
    QuoteSelectionForm,
)
from ..models import Contact, Note, Organisation
from ..portal_checklist import deal_people, outstanding_items
from .shared import CustomerRequiredMixin

logger = logging.getLogger(__name__)


def _log_customer_update(user, section: str, form, *, contact=None, organisation=None) -> None:
    """Audit trail: one Note per saved edit that actually changed something.

    Surfaces in the staff notes streams so staff can see what customers
    changed and when. Named after the actor — a deal can have several
    applicants logged in."""
    if not form.changed_data:
        return
    labels = [str(form.fields[name].label or name) for name in form.changed_data]
    Note.objects.create(
        type=Note.Type.CUSTOMER_UPDATE,
        author=user,
        datetime=timezone.now(),
        content=f"{user.full_name} updated {section}: {', '.join(labels)}.",
        contact=contact,
        organisation=organisation,
    )


def customer_organisations(user):
    """Orgs a customer may see/edit: those on deals where they are the
    primary applicant. Co-applicants never edit the company — and their own
    organisation memberships don't count, since adding someone to a deal
    also adds them to its organisation."""
    return (
        Organisation.objects.filter(deals__customer__user=user)
        .distinct()
        .order_by("name")
    )


def customer_people(user):
    """People a customer may see/edit: themselves, plus the co-applicants on
    deals where they are the primary applicant. A co-applicant therefore
    gets only themselves. Never other contacts of their organisations.
    Tolerates a user with no linked contact (dup-email edge) — they just see
    less."""
    return (
        Contact.objects.filter(
            Q(user=user) | Q(co_applicant_deals__customer__user=user)
        )
        .distinct()
        .order_by("last_name", "first_name")
    )


def customer_deals(user):
    """Deals the customer may see: those where they are the primary applicant
    or a co-applicant (crm.access.portal_deals).

    Prefetches everything `portal_checklist.outstanding_items` reads so the
    deals list can badge "N actions needed" per row without N+1 queries."""
    return (
        portal_deals(user)
        .select_related("customer", "organisation", "selected_quote")
        .prefetch_related(
            "documents", "signature_requests__signer", "quotes", "co_applicants",
        )
        .order_by("-created_at")
    )


class _CustomerCardView(CustomerRequiredMixin, View):
    """Base for one inline-editable section: card + edit endpoints.

    Subclasses set `form_class`, `section` (audit label), `object_name`
    (context key), `card_template`, `form_template`, `page_url` and implement
    `get_queryset()` / `note_kwargs(obj)`. The queryset is the permission
    check: anything a card endpoint can load, this user may edit, so the card
    partials render in their editable state (`card_context`)."""

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

    def card_context(self, obj) -> dict:
        return {}

    def render_card(self, request, obj):
        return render(request, self.card_template, {self.object_name: obj, **self.card_context(obj)})

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
    page_url = "dashboard"

    def get_queryset(self):
        return customer_organisations(self.request.user)

    def card_context(self, obj):
        return {"can_edit_company": True}

    def note_kwargs(self, obj):
        return {"organisation": obj}


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
    page_url = "dashboard"

    def get_queryset(self):
        return customer_people(self.request.user)

    def card_context(self, obj):
        obj.can_edit = True
        return {}

    def note_kwargs(self, obj):
        return {"contact": obj}


class MyPersonCardView(_PersonMixin, _CardPartialView):
    pass


class MyPersonEditView(_PersonMixin, _EditPartialView):
    pass


# --- Deals -----------------------------------------------------------------

class MyDealListView(CustomerRequiredMixin, View):
    """All of the customer's deals, current and past, each badged with how
    many things are still needed on it."""

    def get(self, request):
        deals = list(customer_deals(request.user))
        for deal in deals:
            deal.outstanding = outstanding_items(deal, user=request.user)
        return render(request, "crm/my_deal_list.html", {"deals": deals})


class MyDealDetailView(CustomerRequiredMixin, View):
    """The applicant's home: one deal, everything we need from them on it.

    The page is shaped by the viewer's role. The primary gets every card
    editable plus the add/remove-applicant controls; a co-applicant gets
    their own card editable, other people's names only, the company and
    quote read-only, and a Sign button only on their own requests."""

    def get(self, request, pk):
        deal = get_object_or_404(
            customer_deals(request.user).select_related("owner"), pk=pk
        )
        role = deal_role(deal, request.user)
        is_primary = role == PRIMARY
        amount = deal.finance_amount

        links = {link.contact_id: link for link in deal.co_applicant_links.all()}
        people = deal_people(deal)
        for person in people:
            person.can_edit = is_primary or person.user_id == request.user.pk
            link = links.get(person.pk)
            person.customer_added = bool(link and link.added_by_customer)
            # Only the primary removes, and only people a customer added;
            # staff-added applicants are the representative's call.
            person.removal_blocker = (
                applicants.removal_blocker(deal, person)
                if is_primary and person.customer_added else None
            )
            person.can_remove = is_primary and person.customer_added and not person.removal_blocker

        signature_requests = list(deal.signature_requests.all())
        for sr in signature_requests:
            sr.can_sign = may_sign(sr, request.user, role)

        can_add_applicants = is_primary and deal.organisation_id is not None
        return render(
            request,
            "crm/my_deal_detail.html",
            {
                "deal": deal,
                "role": role,
                "is_primary": is_primary,
                "can_edit_company": is_primary,
                "can_select_quote": is_primary,
                "can_add_applicants": can_add_applicants,
                "applicant_form": CustomerApplicantForm(deal=deal) if can_add_applicants else None,
                "outstanding": outstanding_items(deal, user=request.user),
                "people": people,
                "org": deal.organisation,
                "amount_display": f"£{amount:,.2f}" if amount is not None else None,
                "documents": deal.documents.all(),
                "quotes": deal.customer_quotes,
                "signature_requests": signature_requests,
                "docuseal_ready": docuseal.is_configured(),
                "repayment_schedule": deal.repayment_schedule if deal.first_payment_date else None,
            },
        )


class _PrimaryDealView(CustomerRequiredMixin, View):
    """Base for actions only the primary applicant may take on a deal: load
    it through the portal scope (404 when it isn't theirs at all), then 403
    unless they are its customer contact."""

    def get_primary_deal(self, request, pk):
        deal = get_object_or_404(customer_deals(request.user), pk=pk)
        if deal_role(deal, request.user) != PRIMARY:
            raise PermissionDenied
        return deal


class MyDealQuoteSelectView(_PrimaryDealView):
    """Customer picks (or changes) their quote — the deal page's Quotes card
    posts here. HTMX gets the refreshed card partial back; non-HTMX degrades
    to a redirect. Every change logs a customer_update Note on the deal.
    Primary applicant only: the quote binds the company."""

    def post(self, request, pk):
        deal = self.get_primary_deal(request, pk)
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
                detail = f"{quote.term} months" if quote.term else "terms"
                if monthly is not None:
                    detail += f" — £{monthly:,.2f}/mo"
                Note.objects.create(
                    type=Note.Type.CUSTOMER_UPDATE,
                    author=request.user,
                    datetime=timezone.now(),
                    content=f"{request.user.full_name} selected a quote: {detail}.",
                    deal=deal,
                )
        if is_htmx:
            return render(
                request,
                "crm/_my_quotes_card.html",
                {"deal": deal, "quotes": deal.customer_quotes, "quote_saved": saved,
                 "can_select_quote": True,
                 "quote_error": None if saved else "Please pick one of the quotes."},
            )
        if saved:
            messages.success(request, "Quote selected — thank you.")
        else:
            messages.error(request, "Please pick one of the quotes.")
        return redirect("crm:my_deal_detail", pk=deal.pk)


class MyDealApplicantAddView(_PrimaryDealView):
    """The primary applicant adds someone from their company to the
    application. A plain POST + redirect (the page's flash messages carry the
    outcome). Always creates a new Contact — see crm.applicants — flags it
    as customer-added for staff, logs a deal Note and emails the team."""

    def post(self, request, pk):
        deal = self.get_primary_deal(request, pk)
        page = reverse("crm:my_deal_detail", args=[deal.pk]) + "#people"
        if deal.organisation_id is None:
            messages.error(
                request,
                "We need to set up your company before anyone can be added — "
                "please contact your Medifinance representative.",
            )
            return redirect(page)
        form = CustomerApplicantForm(request.POST, deal=deal)
        if not form.is_valid():
            problems = []
            for field, errors in form.errors.items():
                label = form.fields[field].label if field in form.fields else ""
                problems.append(f"{label}: {errors[0]}" if label else errors[0])
            messages.error(request, "Couldn't add that person. " + " ".join(problems))
            return redirect(page)

        contact = applicants.add_applicant(
            deal,
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            email=form.cleaned_data["email"],
            phone=form.cleaned_data.get("phone") or "",
            by_user=request.user,
            by_customer=True,
        )
        try:
            from accounts.emails import send_applicant_added_email
            send_applicant_added_email(
                deal_name=deal.name,
                deal_url=request.build_absolute_uri(deal.get_absolute_url()),
                added_by_name=request.user.full_name,
                applicant_name=contact.full_name or contact.email,
                applicant_email=contact.email,
            )
        except Exception:
            # The applicant is already saved; a mailserver hiccup shouldn't
            # turn that into an error page for the customer.
            logger.exception("Applicant-added notification failed for deal %s", deal.pk)
        messages.success(
            request,
            f"Added {contact}. Your Medifinance representative can send them their own secure link.",
        )
        return redirect(page)


class MyDealApplicantRemoveView(_PrimaryDealView):
    """The primary applicant removes someone they (or another customer)
    added. Staff-added applicants, and anyone with a signature request or an
    upload on the deal, stay — the message says to contact the
    representative. The Contact itself is never deleted."""

    def post(self, request, pk, contact_pk):
        deal = self.get_primary_deal(request, pk)
        page = reverse("crm:my_deal_detail", args=[deal.pk]) + "#people"
        link = get_object_or_404(
            deal.co_applicant_links.select_related("contact"), contact_id=contact_pk,
        )
        contact = link.contact
        if not link.added_by_customer:
            messages.error(
                request,
                f"{contact} was added by Medifinance — please contact your representative to remove them.",
            )
            return redirect(page)
        blocker = applicants.removal_blocker(deal, contact)
        if blocker:
            messages.error(request, f"{blocker} Please contact your Medifinance representative.")
            return redirect(page)
        applicants.remove_applicant(deal, contact, by_user=request.user, log=True)
        messages.success(request, f"Removed {contact} from the application.")
        return redirect(page)


class MyDealSignView(CustomerRequiredMixin, View):
    """Send the applicant to DocuSeal's signing page for one of their deal's
    signature requests ({DOCUSEAL_URL}/s/{slug}, opened in a new tab from the
    deal page). A redirect rather than an embed — DocuSeal's embedded form
    component needs a Pro subscription on self-hosted instances. This view
    still owns the guards and the lazy slug backfill, so the portal URL is
    stable whatever DocuSeal plan is behind it.

    A request may only be opened by the login of the contact it is addressed
    to (crm.access.may_sign): every applicant can now have their own login,
    so the primary no longer opens a co-applicant's request on their behalf.
    Requests from before signers were recorded stay with the primary."""

    def get(self, request, deal_pk, sr_pk):
        deal = get_object_or_404(customer_deals(request.user), pk=deal_pk)
        sr = get_object_or_404(deal.signature_requests.select_related("signer"), pk=sr_pk)
        if not may_sign(sr, request.user, deal_role(deal, request.user)):
            raise PermissionDenied

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
