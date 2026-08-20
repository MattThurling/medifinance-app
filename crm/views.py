import json
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Case, F, OuterRef, ProtectedError, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest, Lower
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from accounts.models import Role
from accounts.permissions import StaffRequiredMixin

from .forms import (
    CoApplicantFormSet,
    CompanyInfoForm,
    ContactForm,
    CustomerInfoForm,
    DealForm,
    DocumentRequestForm,
    DocumentUploadForm,
    OrganisationForm,
    ParticipationForm,
    ProposalForm,
    RateBandForm,
    RateLookupForm,
    RateUploadForm,
    SignatureRequestForm,
    SupplierInvoiceForm,
    XeroInvoiceForm,
    QuoteForm,
    QuoteSelectionForm,
    StageForm,
)
from . import docuseal, pricing
from .models import (
    Contact,
    Deal,
    Document,
    Note,
    Organisation,
    Participation,
    ParticipationInvoiceLink,
    Proposal,
    Quote,
    RateBand,
    SignatureRequest,
    Stage,
    XeroConnection,
    XeroInvoice,
)


logger = logging.getLogger(__name__)


def _latest(model, ts_field, **filters):
    """Subquery for the newest `ts_field` of a deal's related `model` rows."""
    return Subquery(
        model.objects.filter(deal=OuterRef("pk"), **filters)
        .order_by(f"-{ts_field}")
        .values(ts_field)[:1]
    )


def _deal_summaries(qs):
    """Annotate deals with their current stage code, funded total and latest
    activity so list pages don't have to fire one query per row for the Deal
    properties."""
    latest_stage = (
        Stage.objects.filter(deal=OuterRef("pk")).order_by("-occurred_at", "-pk").values("name")[:1]
    )
    funded = (
        Participation.objects.filter(deal=OuterRef("pk"))
        .values("deal")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    return qs.annotate(
        current_stage_name=Subquery(latest_stage),
        funded_total=Subquery(funded),
        last_stage_at=_latest(Stage, "occurred_at"),
        last_note_at=_latest(Note, "datetime"),
        last_participation_at=_latest(Participation, "created_at"),
        last_proposal_at=_latest(Proposal, "created_at"),
        last_quote_at=_latest(Quote, "created_at"),
        last_document_at=_latest(
            Document, "uploaded_at", status=Document.Status.PROVIDED, uploaded_at__isnull=False
        ),
        last_xero_at=_latest(XeroInvoice, "created_at"),
    ).annotate(
        # On SQLite, Greatest returns NULL if *any* argument is NULL, so each
        # source is coalesced to created_at — which is also the fallback we
        # want for deals with no activity yet.
        last_activity_at=Greatest(
            Coalesce("last_stage_at", "created_at"),
            Coalesce("last_note_at", "created_at"),
            Coalesce("last_participation_at", "created_at"),
            Coalesce("last_proposal_at", "created_at"),
            Coalesce("last_quote_at", "created_at"),
            Coalesce("last_document_at", "created_at"),
            Coalesce("last_xero_at", "created_at"),
        ),
    )


# Priority order for label ties — a stage change often creates sibling records
# in the same instant, so it wins; the stage label itself is derived per row.
_ACTIVITY_LABELS = [
    ("last_stage_at", None),
    ("last_note_at", "Note added"),
    ("last_document_at", "Document uploaded"),
    ("last_participation_at", "Supplier invoice added"),
    ("last_proposal_at", "Proposal added"),
    ("last_quote_at", "Quote added"),
    ("last_xero_at", "Xero invoice raised"),
]


def _attach_activity_labels(deals):
    """Set `last_activity_label` on each deal from `_deal_summaries` annotations."""
    for d in deals:
        d.last_activity_label = "Deal created"
        for attr, label in _ACTIVITY_LABELS:
            if d.last_activity_at is not None and getattr(d, attr) == d.last_activity_at:
                if label is None:
                    try:
                        stage = Stage.Name(d.current_stage_name).label
                    except ValueError:
                        stage = d.current_stage_name or ""
                    label = f"Stage: {stage}" if stage else "Stage changed"
                d.last_activity_label = label
                break


class SearchableListView(ListView):
    """ListView that supports `?q=` filtering via `search_fields`."""

    paginate_by = 25
    search_fields: list[str] = []

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get("q", "").strip()
        if q and self.search_fields:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": q})
            qs = qs.filter(cond)
        return qs.distinct()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class SortableListMixin:
    """Whitelist-based `?sort=` handling. `sort_fields` maps keys to ORM
    expressions; prefix the query value with '-' for descending. Views call
    `apply_sort(qs)` explicitly so ordering happens after any annotations."""

    sort_fields: dict = {}
    default_sort = ""

    def get_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        if sort.lstrip("-") not in self.sort_fields:
            return self.default_sort
        return sort

    def apply_sort(self, qs):
        sort = self.get_sort()
        if not sort:
            return qs
        expr = self.sort_fields[sort.lstrip("-")]
        if sort.startswith("-"):
            ordering = expr.desc(nulls_last=True)
        else:
            ordering = expr.asc(nulls_last=True)
        return qs.order_by(ordering, "-created_at", "-pk")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sort"] = self.get_sort()
        return ctx


class OwnerFilterMixin:
    """Whitelist-based `?owner=` filtering shared by the deal, contact and
    organisation lists: `me`, `none`, or a user pk. Views call
    `apply_owner_filter(qs)` explicitly, alongside their other filters."""

    def apply_owner_filter(self, qs):
        owner = self.request.GET.get("owner", "")
        if owner == "me":
            qs = qs.filter(owner=self.request.user)
        elif owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif owner.isdigit():
            qs = qs.filter(owner_id=owner)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["owner_choices"] = (
            get_user_model()
            .objects.filter(is_active=True, role__in=[Role.ADMIN, Role.ASSOCIATE])
            .order_by("first_name", "last_name")
        )
        ctx["owner_filter"] = self.request.GET.get("owner", "")
        return ctx


class SectorFilterMixin:
    """Whitelist-based `?sector=` filtering: a Sector code or `none`. Views
    call `apply_sector_filter(qs)` explicitly, alongside their other filters."""

    def apply_sector_filter(self, qs):
        sector = self.request.GET.get("sector", "")
        if sector == "none":
            qs = qs.filter(Q(sector__isnull=True) | Q(sector=""))
        elif sector in Organisation.Sector.values:
            qs = qs.filter(sector=sector)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sector_choices"] = Organisation.Sector.choices
        ctx["sector_filter"] = self.request.GET.get("sector", "")
        return ctx


class _OwnerFormMixin:
    """Passes the logged-in user to the form (owner default on create) and
    supplies the owner-combobox selection context."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        owner_id = ctx["form"]["owner"].value()
        ctx["owner_selected_id"] = owner_id or ""
        ctx["owner_selected_label"] = (
            get_user_model().objects.filter(pk=owner_id).first() if owner_id else ""
        )
        return ctx


class ProtectedDeleteMixin:
    """Convert ProtectedError into a friendly redirect with a flash message."""

    protected_redirect_url: str = ""
    protected_message: str = "Can't delete this — other records still reference it."

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ProtectedError:
            messages.error(request, self.protected_message)
            return redirect(self.protected_redirect_url or self.get_object().get_absolute_url())


# --- Organisation -----------------------------------------------------------

class OrganisationListView(
    SortableListMixin, OwnerFilterMixin, SectorFilterMixin, StaffRequiredMixin, SearchableListView
):
    model = Organisation
    search_fields = [
        "name",
        "legal_name",
        "trading_name",
        "companies_house_number",
        "hubspot_id",
    ]
    default_sort = "name"
    sort_fields = {
        "name": Lower("name"),
        "sector": F("sector"),
        "owner": Lower(Coalesce("owner__first_name", Value(""))),
        "created": F("created_at"),
    }

    def get_queryset(self):
        qs = super().get_queryset().select_related("owner")
        qs = self.apply_owner_filter(qs)
        qs = self.apply_sector_filter(qs)
        return self.apply_sort(qs)


class OrganisationDetailView(StaffRequiredMixin, DetailView):
    model = Organisation

    def get_queryset(self):
        return super().get_queryset().select_related("owner")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["contacts"] = self.object.contacts.all()
        ctx["deals"] = _deal_summaries(
            Deal.objects.filter(organisation=self.object).select_related("owner", "customer")
        )
        ctx["notes"] = self.object.notes.select_related("author")
        return ctx


class OrganisationCreateView(StaffRequiredMixin, _OwnerFormMixin, CreateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationUpdateView(StaffRequiredMixin, _OwnerFormMixin, UpdateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationDeleteView(StaffRequiredMixin, ProtectedDeleteMixin, DeleteView):
    model = Organisation
    success_url = reverse_lazy("crm:organisation_list")
    protected_message = "Can't delete this organisation — it still has contacts linked to it."

    @property
    def protected_redirect_url(self):
        return self.object.get_absolute_url()


# --- Contact ----------------------------------------------------------------

class ContactListView(SortableListMixin, OwnerFilterMixin, StaffRequiredMixin, SearchableListView):
    model = Contact
    search_fields = ["first_name", "last_name", "email", "hubspot_id", "organisations__name"]
    default_sort = "name"
    sort_fields = {
        "name": Lower(Coalesce("last_name", Value(""))),
        "email": Lower("email"),
        "owner": Lower(Coalesce("owner__first_name", Value(""))),
        "created": F("created_at"),
    }

    def get_queryset(self):
        qs = super().get_queryset().select_related("owner").prefetch_related("organisations")
        qs = self.apply_owner_filter(qs)
        return self.apply_sort(qs)


class ContactDetailView(StaffRequiredMixin, DetailView):
    model = Contact

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related("user", "owner")
            .prefetch_related("organisations")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["deals"] = _deal_summaries(self.object.deals.select_related("owner", "organisation"))
        ctx["notes"] = self.object.notes.select_related("author")
        return ctx


class ContactCreateView(StaffRequiredMixin, _OwnerFormMixin, CreateView):
    """Create a contact. Accepts `?organisation=<pk>` to prefill + lock the org FK
    (from an organisation's detail page), or `?deal=<pk>` to attach the new contact
    to that deal as a co-applicant (org locked to the deal's customer's org)."""

    model = Contact
    form_class = ContactForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.parent_organisation = None
        self.parent_deal = None

        deal_pk = request.GET.get("deal")
        if deal_pk:
            try:
                self.parent_deal = Deal.objects.select_related("organisation").get(pk=deal_pk)
                self.parent_organisation = self.parent_deal.organisation
            except (Deal.DoesNotExist, ValueError, TypeError):
                pass

        if self.parent_organisation is None:
            org_pk = request.GET.get("organisation")
            if org_pk:
                try:
                    self.parent_organisation = Organisation.objects.get(pk=org_pk)
                except (Organisation.DoesNotExist, ValueError, TypeError):
                    pass

    def get_initial(self):
        initial = super().get_initial()
        if self.parent_organisation:
            initial["organisations"] = [self.parent_organisation]
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_organisation"] = self.parent_organisation
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)  # saves contact + organisations M2M
        if self.parent_deal:
            self.parent_deal.co_applicants.add(self.object)
            if self.parent_deal.organisation_id:
                # Idempotent; covers the locked-org case even when the M2M field
                # wasn't submitted (e.g. if the template renders a hidden input).
                self.object.organisations.add(self.parent_deal.organisation_id)
            messages.success(self.request, f"Added {self.object} as an applicant.")
        return response

    def get_success_url(self):
        if self.parent_deal:
            return self.parent_deal.get_absolute_url()
        if self.parent_organisation:
            return self.parent_organisation.get_absolute_url()
        return self.object.get_absolute_url()


class ContactUpdateView(StaffRequiredMixin, _OwnerFormMixin, UpdateView):
    model = Contact
    form_class = ContactForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class ContactDeleteView(StaffRequiredMixin, ProtectedDeleteMixin, DeleteView):
    model = Contact
    success_url = reverse_lazy("crm:contact_list")
    protected_message = "Can't delete this contact — they're the customer on one or more deals."


# --- Deal -------------------------------------------------------------------

class DealListView(SortableListMixin, OwnerFilterMixin, StaffRequiredMixin, SearchableListView):
    model = Deal
    search_fields = [
        "name",
        "hubspot_id",
        "customer__first_name",
        "customer__last_name",
        "customer__email",
        "organisation__name",
    ]
    default_sort = "-created"
    sort_fields = {
        "name": Lower("name"),
        "type": F("type"),
        # Pipeline order (choice declaration order), not alphabetical-by-code.
        "stage": Case(
            *[
                When(current_stage_name=code, then=Value(i))
                for i, (code, _label) in enumerate(Stage.Name.choices)
            ]
        ),
        "funded": F("funded_total"),
        "owner": Lower(Coalesce("owner__first_name", Value(""))),
        "created": F("created_at"),
        "activity": F("last_activity_at"),
    }

    def get_queryset(self):
        qs = _deal_summaries(
            super().get_queryset().select_related("owner", "customer", "organisation")
        )
        qs = self._apply_filters(qs)
        return self.apply_sort(qs)

    def _apply_filters(self, qs):
        stage = self.request.GET.get("stage", "")
        if stage in Stage.Name.values:
            qs = qs.filter(current_stage_name=stage)
        deal_type = self.request.GET.get("type", "")
        if deal_type == "none":
            qs = qs.filter(Q(type__isnull=True) | Q(type=""))
        elif deal_type in Deal.Type.values:
            qs = qs.filter(type=deal_type)
        return self.apply_owner_filter(qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _attach_activity_labels(ctx["object_list"])
        ctx["stage_choices"] = Stage.Name.choices
        ctx["stage_filter"] = self.request.GET.get("stage", "")
        ctx["type_choices"] = Deal.Type.choices
        ctx["type_filter"] = self.request.GET.get("type", "")
        return ctx


class DealMaturingListView(OwnerFilterMixin, StaffRequiredMixin, ListView):
    """Live deals whose finance term ends within a chosen window — the
    call-before-it-matures worklist for refinance / upgrade business.

    Maturity comes from `Deal.maturity_date` (explicit `term_end_date`, else
    derived from the selected quote), which can't be expressed in portable
    ORM SQL — so the live subset is filtered and sorted in Python. That set
    is a few hundred rows, well within a single page's budget.
    """

    model = Deal
    template_name = "crm/deal_maturing_list.html"
    paginate_by = 50

    WINDOW_CHOICES = (3, 6, 12, 24)  # months
    DEFAULT_WINDOW = 12

    # Python-side equivalents of DealListView.sort_fields — attribute getters
    # rather than ORM expressions, since this list is sorted in memory. Rows
    # whose key is None go last in both directions, like the mixin's
    # nulls_last ordering.
    SORT_KEYS = {
        "name": lambda d: d.name.lower(),
        "type": lambda d: d.type or None,
        "ends": lambda d: d.maturity_date,
        "funded": lambda d: d.funded_total,
        "owner": lambda d: (d.owner.first_name or "").lower() if d.owner else "",
        "activity": lambda d: d.last_activity_at,
    }
    default_sort = "ends"

    def get_window(self) -> int:
        try:
            window = int(self.request.GET.get("window", ""))
        except ValueError:
            return self.DEFAULT_WINDOW
        return window if window in self.WINDOW_CHOICES else self.DEFAULT_WINDOW

    def get_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        if sort.lstrip("-") not in self.SORT_KEYS:
            return self.default_sort
        return sort

    def _live_deals(self):
        qs = _deal_summaries(
            Deal.objects.select_related(
                "owner", "customer", "organisation", "selected_quote"
            )
        ).filter(current_stage_name=Stage.Name.DEAL_LIVE)
        return self.apply_owner_filter(qs)

    def get_queryset(self):
        horizon = pricing.add_months(timezone.localdate(), self.get_window())
        deals = list(self._live_deals())
        self.no_maturity_count = sum(1 for d in deals if d.maturity_date is None)
        maturing = [
            d for d in deals
            if d.maturity_date is not None and d.maturity_date <= horizon
        ]
        sort = self.get_sort()
        keyfn = self.SORT_KEYS[sort.lstrip("-")]
        valued = [d for d in maturing if keyfn(d) is not None]
        nulled = [d for d in maturing if keyfn(d) is None]
        valued.sort(key=keyfn, reverse=sort.startswith("-"))
        return valued + nulled

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _attach_activity_labels(ctx["object_list"])
        ctx["sort"] = self.get_sort()
        ctx["window"] = self.get_window()
        ctx["window_choices"] = self.WINDOW_CHOICES
        ctx["no_maturity_count"] = self.no_maturity_count
        ctx["today"] = timezone.localdate()
        return ctx


class DealDetailView(StaffRequiredMixin, DetailView):
    model = Deal

    def get_queryset(self):
        return super().get_queryset().select_related(
            "owner",
            "customer",
            "organisation",
            "introducer",
        ).prefetch_related(
            "customer__organisations",
            "quotes__rate__organisation",
            "stage_events",
            "co_applicants",
            "documents",
            "signature_requests",
            "participations__organisation",
            "proposals__lender",
            "proposals__contact",
            "xero_invoices",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        deal = self.object

        # Stage history oldest-first for the vertical steps timeline
        ctx["stages_chrono"] = (
            deal.stage_events.select_related("organisation").order_by("occurred_at", "pk")
        )

        # Applicants = the lead (customer, if set) plus any co-applicants
        applicants = [a for a in (deal.customer, *deal.co_applicants.all()) if a]
        ctx["applicants"] = applicants

        # Other contacts in the deal's organisation that staff can attach as applicants
        org = deal.organisation
        ctx["available_applicants"] = (
            org.contacts.exclude(pk__in=[a.pk for a in applicants])
            if org else Contact.objects.none()
        )

        # Split the amount so the pence can render smaller (£25,000.00). The
        # headline card shows what's actually being financed — participations
        # minus the selected quote's deposit and balloon.
        if deal.finance_amount is not None:
            pounds = int(deal.finance_amount)
            pence = int((deal.finance_amount - pounds) * 100)
            ctx["amount_pounds"] = f"{pounds:,}"
            ctx["amount_pence"] = f"{pence:02d}"

        if deal.commission is not None:
            ctx["commission_display"] = f"£{deal.commission:,.2f}"

        # Computed once here — the property walks selected_quote + participations.
        if deal.first_payment_date:
            ctx["repayment_schedule"] = deal.repayment_schedule

        ctx["notes"] = deal.notes.select_related("author")
        ctx["docuseal_configured"] = docuseal.is_configured()
        return ctx


class DealOverviewView(DealDetailView):
    """A read-only, plain-text-style summary of a deal — designed for staff to
    copy individual fields out into emails or other tools."""

    template_name = "crm/deal_overview.html"


class _DealFormMixin(_OwnerFormMixin):
    form_class = DealForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class DealCreateView(StaffRequiredMixin, _DealFormMixin, CreateView):
    """Create a deal. Accepts `?customer=<pk>` to prefill + lock the customer FK
    (when launched from a contact's detail page)."""

    model = Deal

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.parent_contact = None
        contact_pk = request.GET.get("customer")
        if contact_pk:
            try:
                self.parent_contact = Contact.objects.get(pk=contact_pk)
            except (Contact.DoesNotExist, ValueError, TypeError):
                pass

    def get_initial(self):
        initial = super().get_initial()
        if self.parent_contact:
            initial["customer"] = self.parent_contact
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_contact"] = self.parent_contact
        return ctx

    def get_success_url(self):
        if self.parent_contact:
            return self.parent_contact.get_absolute_url()
        return super().get_success_url()


class DealUpdateView(StaffRequiredMixin, _DealFormMixin, UpdateView):
    model = Deal


class DealDeleteView(StaffRequiredMixin, DeleteView):
    model = Deal
    success_url = reverse_lazy("crm:deal_list")


class DealApplicantAddView(StaffRequiredMixin, View):
    """Attach an existing contact from the customer's organisation as a co-applicant."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("organisation"), pk=pk)
        contact = get_object_or_404(Contact, pk=request.POST.get("contact"))
        if deal.organisation_id is None:
            messages.error(request, "Set the deal's organisation before adding applicants.")
        elif not contact.organisations.filter(pk=deal.organisation_id).exists():
            messages.error(request, "You can only add applicants from the deal's organisation.")
        elif contact.pk == deal.customer_id:
            messages.error(request, "That contact is already the lead applicant.")
        else:
            deal.co_applicants.add(contact)
            messages.success(request, f"Added {contact} as an applicant.")
        return redirect(deal.get_absolute_url())


# --- Note -------------------------------------------------------------------

class NoteCreateView(StaffRequiredMixin, View):
    """Add a note from a contact / organisation / deal detail page. UI-added
    notes are always admin comments by the logged-in user, written now."""

    parents = {"contact": Contact, "organisation": Organisation, "deal": Deal}

    def post(self, request):
        given = [f for f in self.parents if request.POST.get(f)]
        if len(given) != 1:
            raise Http404("Provide exactly one of contact, organisation or deal.")
        field = given[0]
        parent = get_object_or_404(self.parents[field], pk=request.POST[field])

        content = request.POST.get("content", "").strip()
        if content:
            from django.utils import timezone

            note = Note(
                type=Note.Type.ADMIN_COMMENT,
                author=request.user,
                content=content,
                datetime=timezone.now(),
                **{field: parent},
            )
            note.save()
            # `datetime` means "when written" — for UI notes that's the moment
            # of creation, so mirror the auto_now_add stamp exactly.
            note.datetime = note.created_at
            note.save(update_fields=["datetime"])
        else:
            messages.error(request, "Note can't be empty.")
        return redirect(parent.get_absolute_url())


# --- Quote -----------------------------------------------------------------
# Quotes are always managed in the context of a parent Deal — no list/detail.

class QuoteCreateView(StaffRequiredMixin, CreateView):
    """Create a quote. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Quote
    form_class = QuoteForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["deal"] = self.parent_deal
        return kwargs

    def form_valid(self, form):
        if self.parent_deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't calculate a monthly payment. "
                "Edit the deal and add a supplier first.",
            )
            return self.form_invalid(form)
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        if self.parent_deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.parent_deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


# --- Customer portal --------------------------------------------------------

class _PortalLinkMixin(StaffRequiredMixin):
    """Shared logic for minting a customer magic link from a Deal.

    Auto-creates (or reuses) the customer's User account, then issues a
    single-use MagicLink. Adds error/warning flash messages as needed.
    Returns (link, full_url) on success, or (None, None) on failure.
    """

    def issue_link(self, request, deal):
        from .portal_links import issue_portal_link_for_deal, NoCustomerEmailError
        try:
            link, dup_warning = issue_portal_link_for_deal(deal, created_by=request.user)
        except NoCustomerEmailError as exc:
            messages.error(request, str(exc))
            return None, None
        if dup_warning:
            messages.warning(request, dup_warning)
        full_url = request.build_absolute_uri(reverse("consume_magic_link", args=[link.token]))
        return link, full_url


class IssuePortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and show its URL to staff for manual sending."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("customer"), pk=pk)
        link, full_url = self.issue_link(request, deal)
        if link is None:
            return redirect(deal.get_absolute_url())
        messages.success(
            request,
            f"Portal link generated for {deal.customer.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}. URL: {full_url}",
        )
        return redirect(deal.get_absolute_url())


class EmailPortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and email it to the customer via the configured backend."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("customer", "owner"), pk=pk)
        link, full_url = self.issue_link(request, deal)
        if link is None:
            return redirect(deal.get_absolute_url())

        from accounts.emails import send_magic_link_email
        send_magic_link_email(
            to_email=link.user.email,
            link_url=full_url,
            deal_name=deal.name,
            owner_name=deal.owner.full_name if deal.owner else "The team",
            expires_at=link.expires_at,
        )
        messages.success(
            request,
            f"Portal link emailed to {link.user.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}.",
        )
        return redirect(deal.get_absolute_url())


class CustomerRequiredMixin(LoginRequiredMixin):
    """Mixin: 403 unless the user has the `customer` role."""

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_customer):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class _SearchView(StaffRequiredMixin, View):
    """Base for HTMX combobox search endpoints. Returns an HTML fragment of
    up to `limit` matches rendered as clickable options."""

    model = None
    search_fields: list[str] = []
    limit = 20

    def get_queryset(self):
        return self.model.objects.all()

    def get(self, request):
        from django.shortcuts import render

        q = request.GET.get("q", "").strip()
        results = []
        if q:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": q})
            results = list(self.get_queryset().filter(cond).distinct()[: self.limit])
        return render(request, "crm/_combobox_results.html", {"results": results})


class ContactSearchView(_SearchView):
    model = Contact
    search_fields = ["first_name", "last_name", "email", "organisations__name"]

    def get_queryset(self):
        return Contact.objects.prefetch_related("organisations")


class OrganisationSearchView(_SearchView):
    model = Organisation
    search_fields = ["name", "legal_name", "trading_name", "companies_house_number"]


class UserSearchView(_SearchView):
    """Search staff users (admins + associates) for the deal owner combobox."""

    search_fields = ["first_name", "last_name", "email"]

    def get_queryset(self):
        return get_user_model().objects.filter(role__in=[Role.ADMIN, Role.ASSOCIATE])


class _PortalDealMixin(CustomerRequiredMixin):
    """Shared queryset filter for portal views — only deals belonging to the user."""

    def get_deal(self, pk):
        return get_object_or_404(
            Deal.objects.filter(customer__user=self.request.user)
            .select_related("owner", "customer", "organisation")
            .prefetch_related("quotes", "stage_events"),
            pk=pk,
        )


class _PortalStepMixin(_PortalDealMixin):
    """A step in the customer application wizard. Steps after Quotes require a
    quote to have been selected; otherwise we bounce back to the Quotes step."""

    step = 1

    def dispatch(self, request, *args, **kwargs):
        if self.step > 1 and request.user.is_authenticated and getattr(request.user, "is_customer", False):
            deal = Deal.objects.filter(customer__user=request.user, pk=kwargs["pk"]).first()
            if deal and not deal.selected_quote_id:
                return redirect("crm:portal_quote_select", pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)


class PortalQuoteSelectView(_PortalStepMixin, View):
    """Step 1: customer picks a quote."""

    step = 1
    template_name = "crm/portal_quote_select.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        form = QuoteSelectionForm(deal=deal, initial={"quote": deal.selected_quote_id})
        return self._render(request, deal, form)

    def post(self, request, pk):
        deal = self.get_deal(pk)
        form = QuoteSelectionForm(request.POST, deal=deal)
        if form.is_valid():
            deal.selected_quote = form.cleaned_data["quote"]
            deal.save(update_fields=["selected_quote"])
            return redirect("crm:portal_company", pk=deal.pk)
        return self._render(request, deal, form)

    def _render(self, request, deal, form):
        from django.shortcuts import render
        amount_display = f"£{deal.finance_amount:,.2f}" if deal.finance_amount is not None else None
        return render(
            request,
            self.template_name,
            {"deal": deal, "form": form, "amount_display": amount_display, "current_step": self.step},
        )


class PortalCompanyView(_PortalStepMixin, View):
    """Step 2: company details (organisation address)."""

    step = 2
    template_name = "crm/portal_company.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        form = CompanyInfoForm(instance=deal.organisation, prefix="company")
        return self._render(request, deal, form)

    def post(self, request, pk):
        deal = self.get_deal(pk)
        form = CompanyInfoForm(request.POST, instance=deal.organisation, prefix="company")
        if form.is_valid():
            org = form.save()
            # Link the saved org to the deal (if not already) and to the customer's
            # organisations M2M (add is idempotent).
            if deal.organisation_id != org.pk:
                deal.organisation = org
                deal.save(update_fields=["organisation"])
            deal.customer.organisations.add(org)
            return redirect("crm:portal_applicants", pk=deal.pk)
        return self._render(request, deal, form)

    def _render(self, request, deal, form):
        from django.shortcuts import render
        return render(
            request,
            self.template_name,
            {"deal": deal, "form": form, "current_step": self.step},
        )


class PortalApplicantsView(_PortalStepMixin, View):
    """Step 3: the lead's details + any co-applicants. Records 'Info Received'."""

    step = 3
    template_name = "crm/portal_applicants.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        return self._render(request, deal, *self._build_forms(deal))

    def post(self, request, pk):
        deal = self.get_deal(pk)
        customer_form, co_formset = self._build_forms(deal, data=request.POST)
        if customer_form.is_valid() and co_formset.is_valid():
            customer_form.save()

            # Save co-applicants. Forms marked DELETE get unlinked (not deleted).
            # Newly-saved contacts get the deal's organisation added to their M2M
            # so they're discoverable under that org going forward.
            org = deal.organisation
            active_contacts = []
            for form in co_formset:
                if form.cleaned_data.get("DELETE"):
                    continue
                if not form.has_changed() and form.instance.pk is None:
                    continue
                contact = form.save(commit=False)
                is_new = contact.pk is None
                contact.save()
                if is_new and org is not None:
                    contact.organisations.add(org)
                active_contacts.append(contact)
            deal.co_applicants.set(active_contacts)

            # Record an Info Received stage event automatically (once).
            if (deal.current_stage is None) or (deal.current_stage.name != Stage.Name.INFO_RECEIVED):
                Stage.objects.create(
                    deal=deal,
                    name=Stage.Name.INFO_RECEIVED,
                    organisation=deal.organisation,
                    set_by=request.user,
                    note="Customer completed application via portal.",
                )
                try:
                    from accounts.emails import send_customer_application_submitted_email
                    finance_amount = deal.finance_amount
                    send_customer_application_submitted_email(
                        deal_name=deal.name,
                        deal_url=request.build_absolute_uri(deal.get_absolute_url()),
                        customer_name=deal.customer.full_name,
                        customer_email=deal.customer.email,
                        organisation_name=deal.organisation.name if deal.organisation else "",
                        amount_display=f"£{finance_amount:,.2f}" if finance_amount is not None else "",
                    )
                except Exception:
                    logger.exception("Customer-application notification email failed for deal %s", deal.pk)

            return redirect("crm:portal_documents", pk=deal.pk)
        return self._render(request, deal, customer_form, co_formset)

    def _build_forms(self, deal, data=None):
        customer_form = CustomerInfoForm(data, instance=deal.customer, prefix="customer")
        co_formset = CoApplicantFormSet(data, queryset=deal.co_applicants.all(), prefix="co")
        return customer_form, co_formset

    def _render(self, request, deal, customer_form, co_formset):
        from django.shortcuts import render
        return render(
            request,
            self.template_name,
            {
                "deal": deal,
                "customer_form": customer_form,
                "co_formset": co_formset,
                "current_step": self.step,
            },
        )


class PortalApplicationCompleteView(_PortalStepMixin, View):
    """Final thank-you page after the wizard is complete."""

    step = 4

    def get(self, request, pk):
        from django.shortcuts import render
        deal = self.get_deal(pk)
        return render(
            request,
            "crm/portal_application_complete.html",
            {"deal": deal, "repayment_schedule": deal.repayment_schedule},
        )


class QuoteUpdateView(StaffRequiredMixin, UpdateView):
    model = Quote
    form_class = QuoteForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["deal"] = self.object.deal
        return kwargs

    def form_valid(self, form):
        if form.instance.deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't recalculate the monthly payment. "
                "Edit the deal and add a supplier first.",
            )
            return self.form_invalid(form)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        if self.object.deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.object.deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class QuoteDeleteView(StaffRequiredMixin, DeleteView):
    model = Quote

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class QuoteSelectView(StaffRequiredMixin, View):
    """Staff equivalent of the portal quote-pick step — sets `Deal.selected_quote`
    from a radio button on the deal detail page."""

    def post(self, request, pk):
        quote = get_object_or_404(Quote.objects.select_related("deal"), pk=pk)
        Deal.objects.filter(pk=quote.deal_id).update(selected_quote=quote)
        return redirect(quote.deal.get_absolute_url())


# --- Stage -----------------------------------------------------------------
# Stages are an immutable event log — only Create. To "undo" a stage change,
# just add another stage event with the previous value.

class StageCreateView(StaffRequiredMixin, CreateView):
    """Record a stage change. Requires `?deal=<pk>`."""

    model = Stage
    form_class = StageForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        form.instance.set_by = self.request.user
        # Manual stages default to the deal's organisation (the client) — staff
        # can add more granular stages via the auto-fired events on Proposals
        # and Participations instead.
        form.instance.organisation = self.parent_deal.organisation
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


# --- Proposal --------------------------------------------------------------
# Map a Proposal.Status -> the Stage.Name to emit when a proposal is created
# with, or its status flips to, that status. WITHDRAWN deliberately absent:
# withdrawing one proposal doesn't move the deal's stage — the proposal's own
# status records it.
PROPOSAL_STATUS_TO_STAGE = {
    Proposal.Status.SUBMITTED: Stage.Name.PROPOSAL_SUBMITTED,
    Proposal.Status.APPROVED: Stage.Name.PROPOSAL_APPROVED,
    Proposal.Status.DECLINED: Stage.Name.PROPOSAL_DECLINED,
}


# Proposals are managed in the context of a parent Deal (the broker shops the
# deal to many lenders), same shape as Quote — no separate list/detail page.

class ProposalSelectView(StaffRequiredMixin, View):
    """Set `Deal.selected_proposal` from a radio button on the deal detail page."""

    def post(self, request, pk):
        proposal = get_object_or_404(Proposal.objects.select_related("deal"), pk=pk)
        Deal.objects.filter(pk=proposal.deal_id).update(selected_proposal=proposal)
        return redirect(proposal.deal.get_absolute_url())


class ProposalCreateView(StaffRequiredMixin, CreateView):
    """Create a proposal. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Proposal
    form_class = ProposalForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        response = super().form_valid(form)
        stage_name = PROPOSAL_STATUS_TO_STAGE.get(self.object.status)
        if stage_name:
            Stage.objects.create(
                deal=self.parent_deal,
                name=stage_name,
                organisation=self.object.lender,
                set_by=self.request.user,
            )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class ProposalUpdateView(StaffRequiredMixin, UpdateView):
    model = Proposal
    form_class = ProposalForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal", "lender", "contact")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def form_valid(self, form):
        old_status = form.initial.get("status")
        response = super().form_valid(form)
        new_status = self.object.status
        if old_status != new_status:
            stage_name = PROPOSAL_STATUS_TO_STAGE.get(new_status)
            if stage_name:
                Stage.objects.create(
                    deal=self.object.deal,
                    name=stage_name,
                    organisation=self.object.lender,
                    set_by=self.request.user,
                )
        return response

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


# --- Participation (Supplier) ----------------------------------------------
# Participations represent suppliers contributing to a deal's funded amount.
# Managed in the context of a parent Deal, same pattern as Quote / Proposal.

class ParticipationCreateView(StaffRequiredMixin, CreateView):
    """Create a participation. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Participation
    form_class = ParticipationForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_form_kwargs(self):
        return super().get_form_kwargs() | {"deal": self.parent_deal}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class ParticipationUpdateView(StaffRequiredMixin, UpdateView):
    model = Participation
    form_class = ParticipationForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal", "organisation", "invoice_contact")

    def get_form_kwargs(self):
        return super().get_form_kwargs() | {"deal": self.object.deal}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class ParticipationDeleteView(StaffRequiredMixin, DeleteView):
    """Staff: remove a participation (POST only — inline, no confirm page)."""

    model = Participation

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class ParticipationInvoiceDownloadView(StaffRequiredMixin, View):
    """Stream a supplier's invoice PDF inline. Goes through Django so the
    private GCS bucket stays private — same pattern as DocumentDownloadView."""

    def get(self, request, pk):
        p = get_object_or_404(Participation.objects.select_related("deal"), pk=pk)
        if not p.invoice:
            raise Http404("No invoice file uploaded yet.")
        filename = p.invoice.name.rsplit("/", 1)[-1]
        return FileResponse(p.invoice.open("rb"), as_attachment=False, filename=filename)


class RequestParticipationInvoiceView(StaffRequiredMixin, View):
    """Staff: mint a single-use upload link for a supplier and email it to their
    invoice contact. Requires a selected Proposal on the deal so the email body
    can name the accepted lender."""

    def post(self, request, pk):
        participation = get_object_or_404(
            Participation.objects.select_related(
                "deal", "deal__organisation", "deal__customer",
                "deal__selected_proposal", "deal__selected_proposal__lender",
                "organisation", "invoice_contact",
            ),
            pk=pk,
        )

        contact = participation.invoice_contact
        if contact is None or not contact.email:
            messages.error(
                request,
                "Set an invoice contact (with an email address) on this supplier first.",
            )
            return redirect(participation.deal.get_absolute_url())

        selected = participation.deal.selected_proposal
        if selected is None:
            messages.error(
                request,
                "Select a Proposal on this deal before sending an invoice request — "
                "the email needs to name the accepted lender.",
            )
            return redirect(participation.deal.get_absolute_url())

        client_org = participation.deal.organisation
        if client_org is None:
            messages.error(
                request,
                "Set the deal's Organisation before sending an invoice request.",
            )
            return redirect(participation.deal.get_absolute_url())

        link = ParticipationInvoiceLink.issue(
            participation=participation, created_by=request.user
        )
        full_url = request.build_absolute_uri(
            reverse("crm:participation_submit_invoice", args=[link.token])
        )

        from accounts.emails import send_supplier_invoice_request_email
        send_supplier_invoice_request_email(
            to_email=contact.email,
            link_url=full_url,
            contact_first_name=(contact.first_name or "").strip(),
            lead_contact_name=str(participation.deal.customer),
            client_org_name=client_org.name,
            client_org_address=client_org.display_address,
            lender_org_name=selected.lender.name,
            lender_org_address=selected.lender.display_address,
            participation_amount_display=f"£{participation.amount:,.2f}",
            participation_description=(participation.description or "").strip(),
            expires_at=link.expires_at,
        )

        Stage.objects.create(
            deal=participation.deal,
            name=Stage.Name.SUPPLIER_INVOICE_REQUESTED,
            organisation=participation.organisation,
            set_by=request.user,
        )

        messages.success(
            request,
            f"Invoice request emailed to {contact.email}. "
            f"Link expires {link.expires_at:%d %b %Y, %H:%M}.",
        )
        return redirect(participation.deal.get_absolute_url())


class RequestDealCommissionInvoiceView(StaffRequiredMixin, View):
    """Staff: notify the ACCOUNTS_EMAILS list to raise a commission invoice for
    this deal. Requires a selected Proposal so the email names the lender."""

    def post(self, request, pk):
        deal = get_object_or_404(
            Deal.objects.select_related(
                "organisation", "customer",
                "selected_proposal", "selected_proposal__lender",
            ),
            pk=pk,
        )

        selected = deal.selected_proposal
        if selected is None:
            messages.error(
                request,
                "Select a Proposal on this deal before requesting a commission invoice.",
            )
            return redirect(deal.get_absolute_url())

        finance = deal.finance_amount
        finance_display = f"£{finance:,.2f}" if finance is not None else "—"
        commission_display = (
            f"£{deal.commission:,.2f}" if deal.commission is not None else "—"
        )

        from accounts.emails import send_commission_invoice_request_email
        send_commission_invoice_request_email(
            deal_name=deal.name,
            deal_url=request.build_absolute_uri(deal.get_absolute_url()),
            customer_name=str(deal.customer),
            client_org_name=deal.organisation.name if deal.organisation else "—",
            lender_org_name=selected.lender.name,
            proposal_number=selected.proposal_number or "—",
            finance_amount_display=finance_display,
            commission_display=commission_display,
            requested_by=request.user.full_name,
        )

        messages.success(request, "Commission invoice request sent to accounts.")
        return redirect(deal.get_absolute_url())


class ProposalNotifyClientView(StaffRequiredMixin, View):
    """Email the deal's customer the good news about an approved proposal.

    Only allowed for the deal's selected proposal while it is Approved, and
    only once — `Proposal.notified_at` records (and blocks repeating) the send.
    """

    def post(self, request, pk):
        proposal = get_object_or_404(
            Proposal.objects.select_related(
                "deal", "deal__customer", "deal__selected_quote", "lender",
            ),
            pk=pk,
        )
        deal = proposal.deal

        if proposal.status != Proposal.Status.APPROVED:
            messages.error(request, "Only an approved proposal can be sent to the client.")
            return redirect(deal.get_absolute_url())
        if deal.selected_proposal_id != proposal.pk:
            messages.error(
                request,
                "Mark this proposal as the deal's selected proposal before notifying the client.",
            )
            return redirect(deal.get_absolute_url())
        if proposal.notified_at is not None:
            messages.error(
                request,
                f"The client was already notified on {proposal.notified_at:%d %b %Y}.",
            )
            return redirect(deal.get_absolute_url())
        if deal.customer is None or not deal.customer.email:
            messages.error(
                request,
                "Can't notify the client: this deal's customer has no email address on file.",
            )
            return redirect(deal.get_absolute_url())

        finance = deal.finance_amount
        quote = deal.selected_quote
        monthly = quote.monthly_payment if quote else None

        from accounts.emails import send_proposal_approved_client_email
        send_proposal_approved_client_email(
            to_email=deal.customer.email,
            contact_first_name=deal.customer.first_name,
            lender_org_name=proposal.lender.name,
            proposal_number=proposal.proposal_number,
            finance_amount_display=f"£{finance:,.2f}" if finance is not None else "",
            term_display=f"{quote.term} months" if quote else "",
            monthly_payment_display=f"£{monthly:,.2f}" if monthly is not None else "",
        )

        proposal.notified_at = timezone.now()
        proposal.save(update_fields=["notified_at", "updated_at"])
        Stage.objects.create(
            deal=deal,
            name=Stage.Name.CLIENT_NOTIFIED,
            organisation=proposal.lender,
            set_by=request.user,
        )
        messages.success(request, f"Approval email sent to {deal.customer.email}.")
        return redirect(deal.get_absolute_url())


class SubmitParticipationInvoiceView(View):
    """Public — the supplier follows the email link, lands here, and uploads
    their invoice. The token is the auth; no login required."""

    template_name = "crm/supplier_invoice_submit.html"
    invalid_template_name = "crm/supplier_invoice_invalid.html"
    complete_template_name = "crm/supplier_invoice_complete.html"

    def _get_link(self, token):
        return ParticipationInvoiceLink.objects.select_related(
            "participation", "participation__deal", "participation__deal__organisation"
        ).filter(token=token).first()

    def _client_ip(self, request):
        # Cloud Run terminates TLS; X-Forwarded-For has the original client.
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")

    def get(self, request, token):
        link = self._get_link(token)
        if link is None:
            return render(request, self.invalid_template_name,
                          {"reason": "We can't find that invoice link — it may have been mistyped."},
                          status=404)
        if link.is_consumed:
            return render(request, self.invalid_template_name,
                          {"reason": "This link has already been used."}, status=410)
        if link.is_expired:
            return render(request, self.invalid_template_name,
                          {"reason": "This link has expired."}, status=410)

        form = SupplierInvoiceForm(instance=link.participation)
        return render(request, self.template_name,
                      {"form": form, "link": link, "participation": link.participation})

    def post(self, request, token):
        link = self._get_link(token)
        if link is None or not link.is_valid:
            reason = ("This link has already been used." if link and link.is_consumed
                      else "This link has expired." if link and link.is_expired
                      else "We can't find that invoice link.")
            return render(request, self.invalid_template_name, {"reason": reason}, status=410)

        form = SupplierInvoiceForm(request.POST, request.FILES, instance=link.participation)
        if form.is_valid():
            form.save()
            link.consume(ip=self._client_ip(request))
            participation = link.participation
            deal = participation.deal
            Stage.objects.create(
                deal=deal,
                name=Stage.Name.SUPPLIER_INVOICE_RECEIVED,
                organisation=participation.organisation,
                # No set_by — the supplier isn't a logged-in user.
            )
            try:
                from accounts.emails import send_supplier_invoice_submitted_email
                send_supplier_invoice_submitted_email(
                    deal_name=deal.name,
                    deal_url=request.build_absolute_uri(deal.get_absolute_url()),
                    supplier_name=participation.organisation.name if participation.organisation else "",
                    client_org_name=deal.organisation.name if deal.organisation else "",
                    amount_display=f"£{participation.amount:,.2f}",
                    invoice_number=participation.invoice_number or "",
                )
            except Exception:
                logger.exception("Supplier-invoice notification email failed for participation %s", participation.pk)
            return render(request, self.complete_template_name,
                          {"participation": participation})
        return render(request, self.template_name,
                      {"form": form, "link": link, "participation": link.participation})


# --- Documents -------------------------------------------------------------

def _can_access_document(user, doc: Document) -> bool:
    """Staff see any document; a customer only their own deal's."""
    if not user.is_authenticated:
        return False
    if user.is_admin or user.is_associate:
        return True
    return user.is_customer and doc.deal.customer.user_id == user.id


class DocumentCreateView(StaffRequiredMixin, CreateView):
    """Staff: add a document request to a deal (file uploaded later)."""

    model = Document
    form_class = DocumentRequestForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class DocumentDeleteView(StaffRequiredMixin, DeleteView):
    """Staff: remove a document request (POST only — inline, no confirm page)."""

    model = Document

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class DocumentUploadView(LoginRequiredMixin, View):
    """Attach a file to a document request. Used by both staff (deal page) and
    the customer (portal). Flips the document to 'provided'."""

    def post(self, request, pk):
        doc = get_object_or_404(Document.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_document(request.user, doc):
            raise PermissionDenied

        form = DocumentUploadForm(request.POST, request.FILES, instance=doc)
        if form.is_valid():
            doc.attach(form.cleaned_data["file"], by=request.user)
            messages.success(request, f"“{doc.name}” uploaded.")
        else:
            messages.error(request, f"Couldn't upload “{doc.name}”. Please choose a file.")

        if request.user.is_customer:
            return redirect("crm:portal_documents", pk=doc.deal_id)
        return redirect(doc.deal.get_absolute_url())


class DocumentDownloadView(LoginRequiredMixin, View):
    """Stream a document's file through Django so access is permission-checked
    (the storage bucket itself stays private)."""

    def get(self, request, pk):
        doc = get_object_or_404(Document.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_document(request.user, doc):
            raise PermissionDenied
        if not doc.file:
            raise Http404("No file has been uploaded for this document yet.")
        filename = doc.file.name.rsplit("/", 1)[-1]
        return FileResponse(doc.file.open("rb"), as_attachment=False, filename=filename)


class PortalDocumentsView(_PortalStepMixin, View):
    """Step 4: customer sees required documents for their deal and uploads them."""

    step = 4
    template_name = "crm/portal_documents.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        return render(
            request,
            self.template_name,
            {
                "deal": deal,
                "documents": deal.documents.all(),
                "upload_form": DocumentUploadForm(),
                "current_step": self.step,
            },
        )


# --- DocuSeal e-signing ----------------------------------------------------

@method_decorator(csrf_exempt, name="dispatch")
class DocuSealWebhookView(View):
    """Receives DocuSeal's form.* events and advances the matching
    SignatureRequest. Auth is a shared secret header configured on the
    DocuSeal console (no session, so CSRF is exempted).

    Contract with DocuSeal's retry loop: 200 acknowledges (including events
    we don't care about or can't match — retrying those can never help),
    4xx means the request itself is bad, 502 asks for a retry (we couldn't
    reach DocuSeal back to fetch the signed files)."""

    def post(self, request):
        if not settings.DOCUSEAL_WEBHOOK_SECRET:
            return JsonResponse({"error": "unconfigured"}, status=503)
        if not constant_time_compare(
            request.headers.get("X-Docuseal-Secret", ""),
            settings.DOCUSEAL_WEBHOOK_SECRET,
        ):
            return JsonResponse({"error": "forbidden"}, status=401)

        try:
            payload = json.loads(request.body)
            event = payload["event_type"]
            data = payload.get("data") or {}
        except (ValueError, TypeError, KeyError):
            return JsonResponse({"error": "bad_payload"}, status=400)

        # form.* events carry the submitter (with its parent submission_id);
        # submission.* events carry the submission itself.
        submission_id = data.get("submission_id") if event.startswith("form.") else data.get("id")
        if not submission_id:
            return JsonResponse({"ok": True})

        try:
            with transaction.atomic():
                sr = (
                    SignatureRequest.objects
                    .select_for_update()
                    .filter(submission_id=submission_id)
                    .first()
                )
                if sr is None:
                    logger.info("DocuSeal webhook %s for unknown submission %s", event, submission_id)
                elif event in ("form.viewed", "form.started"):
                    if sr.status == SignatureRequest.Status.SENT:
                        sr.status = SignatureRequest.Status.OPENED
                        sr.opened_at = timezone.now()
                        sr.save(update_fields=["status", "opened_at", "updated_at"])
                elif event == "form.declined":
                    if sr.status not in (SignatureRequest.Status.COMPLETED, SignatureRequest.Status.VOIDED):
                        sr.status = SignatureRequest.Status.DECLINED
                        sr.declined_at = timezone.now()
                        sr.decline_reason = data.get("decline_reason") or ""
                        sr.save(update_fields=["status", "declined_at", "decline_reason", "updated_at"])
                elif event in ("form.completed", "submission.completed"):
                    self._handle_completed(sr, data)
        except docuseal.DocuSealError as exc:
            logger.error("DocuSeal webhook for submission %s: %s", submission_id, exc)
            return JsonResponse({"error": "docuseal_unreachable"}, status=502)

        return JsonResponse({"ok": True})

    @staticmethod
    def _handle_completed(sr: SignatureRequest, data: dict) -> None:
        # Duplicate delivery (or form.completed followed by submission.completed)
        # must be a no-op once the signed file is in.
        if sr.status == SignatureRequest.Status.COMPLETED and sr.signed_file:
            return

        # Verify-then-fetch: treat our own API call as the source of truth
        # rather than the webhook body someone POSTed at us.
        submission = docuseal.get_submission(sr.submission_id)
        if submission.get("status") != "completed" and not submission.get("completed_at"):
            logger.warning(
                "DocuSeal sent a completed event for submission %s but the API says %r",
                sr.submission_id, submission.get("status"),
            )
            return

        files = docuseal.get_submission_documents(sr.submission_id)
        if files:
            pdf = docuseal.download_file(files[0]["url"])
            base = slugify(sr.template_name) or "agreement"
            sr.signed_file.save(f"{base}-signed.pdf", ContentFile(pdf), save=False)

        audit_url = submission.get("audit_log_url") or (data.get("submission") or {}).get("audit_log_url")
        if audit_url and not sr.audit_log_file:
            sr.audit_log_file.save("audit-log.pdf", ContentFile(docuseal.download_file(audit_url)), save=False)

        submitter = (submission.get("submitters") or [{}])[0]
        sr.status = SignatureRequest.Status.COMPLETED
        sr.completed_at = timezone.now()
        sr.signer_ip = submitter.get("ip") or data.get("ip") or None
        sr.signer_user_agent = (submitter.get("ua") or data.get("ua") or "")[:512]
        sr.save()


class SignatureRequestCreateView(StaffRequiredMixin, View):
    """Staff: send a deal off for e-signature."""

    template_name = "crm/signature_request_form.html"

    def dispatch(self, request, *args, pk=None, **kwargs):
        self.deal = get_object_or_404(Deal.objects.select_related("customer"), pk=pk)
        if not docuseal.is_configured():
            messages.error(request, "DocuSeal isn't configured on this environment yet.")
            return redirect(self.deal.get_absolute_url())
        return super().dispatch(request, *args, **kwargs)

    def _signers(self):
        pks = [c.pk for c in (self.deal.customer, *self.deal.co_applicants.all()) if c]
        return Contact.objects.filter(pk__in=pks)

    def _form(self, data=None):
        try:
            templates = docuseal.list_templates()
        except docuseal.DocuSealError as exc:
            messages.error(self.request, f"Couldn't reach DocuSeal: {exc}")
            return None
        if not templates:
            messages.error(self.request, "No templates exist on DocuSeal yet — build one there first.")
            return None
        customer = self.deal.customer
        initial = {
            "signer": customer,
            "signer_email": customer.email if customer else "",
            "signer_name": customer.full_name if customer else "",
        }
        return SignatureRequestForm(data, initial=initial, templates=templates, signers=self._signers())

    def get(self, request, **kwargs):
        form = self._form()
        if form is None:
            return redirect(self.deal.get_absolute_url())
        return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

    def post(self, request, **kwargs):
        form = self._form(request.POST)
        if form is None:
            return redirect(self.deal.get_absolute_url())
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

        template_id = int(form.cleaned_data["template"])
        template_name = dict(form.fields["template"].choices).get(template_id, "")
        try:
            result = docuseal.create_submission(
                template_id=template_id,
                signer_email=form.cleaned_data["signer_email"],
                signer_name=form.cleaned_data["signer_name"],
                values=docuseal.build_prefill_values(self.deal),
                message=form.cleaned_data["message"] or None,
            )
        except docuseal.DocuSealError as exc:
            messages.error(request, f"DocuSeal rejected the request: {exc}")
            return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

        SignatureRequest.objects.create(
            deal=self.deal,
            template_id=template_id,
            template_name=template_name,
            submission_id=result["submission_id"],
            submitter_id=result["submitter_id"],
            signer=form.cleaned_data["signer"],
            signer_email=form.cleaned_data["signer_email"],
            signer_name=form.cleaned_data["signer_name"],
            created_by=request.user,
        )
        messages.success(
            request,
            f"“{template_name or 'Signature request'}” sent to {form.cleaned_data['signer_email']} for signature.",
        )
        return redirect(self.deal.get_absolute_url())


class SignatureRequestVoidView(StaffRequiredMixin, View):
    """Staff: cancel an in-flight signature request (POST only — inline)."""

    def post(self, request, pk):
        sr = get_object_or_404(SignatureRequest.objects.select_related("deal"), pk=pk)
        if not sr.is_active:
            messages.error(request, "Only a pending signature request can be voided.")
            return redirect(sr.deal.get_absolute_url())
        try:
            docuseal.archive_submission(sr.submission_id)
        except docuseal.DocuSealError as exc:
            messages.error(request, f"Couldn't void on DocuSeal: {exc}")
            return redirect(sr.deal.get_absolute_url())
        sr.status = SignatureRequest.Status.VOIDED
        sr.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Signature request “{sr.template_name or sr.submission_id}” voided.")
        return redirect(sr.deal.get_absolute_url())


class SignatureRequestResendView(StaffRequiredMixin, View):
    """Staff: re-send a pending or declined request — voids the old DocuSeal
    submission and creates a fresh one to the same signer (POST only)."""

    def post(self, request, pk):
        old = get_object_or_404(SignatureRequest.objects.select_related("deal"), pk=pk)
        deal = old.deal
        if old.status == SignatureRequest.Status.COMPLETED:
            messages.error(request, "That request has already been completed.")
            return redirect(deal.get_absolute_url())
        try:
            if old.is_active:
                docuseal.archive_submission(old.submission_id)
            result = docuseal.create_submission(
                template_id=old.template_id,
                signer_email=old.signer_email,
                signer_name=old.signer_name,
                values=docuseal.build_prefill_values(deal),
            )
        except docuseal.DocuSealError as exc:
            messages.error(request, f"Couldn't re-send via DocuSeal: {exc}")
            return redirect(deal.get_absolute_url())
        if old.is_active:
            old.status = SignatureRequest.Status.VOIDED
            old.save(update_fields=["status", "updated_at"])
        SignatureRequest.objects.create(
            deal=deal,
            template_id=old.template_id,
            template_name=old.template_name,
            submission_id=result["submission_id"],
            submitter_id=result["submitter_id"],
            signer=old.signer,
            signer_email=old.signer_email,
            signer_name=old.signer_name,
            created_by=request.user,
        )
        messages.success(
            request,
            f"“{old.template_name or 'Signature request'}” re-sent to {old.signer_email} for signature.",
        )
        return redirect(deal.get_absolute_url())


class SignatureSignedFileDownloadView(StaffRequiredMixin, View):
    """Stream the signed PDF for a completed signature request."""

    def get(self, request, pk):
        sr = get_object_or_404(SignatureRequest, pk=pk)
        if not sr.signed_file:
            raise Http404("No signed document has been stored for this signature request.")
        filename = sr.signed_file.name.rsplit("/", 1)[-1]
        return FileResponse(sr.signed_file.open("rb"), as_attachment=False, filename=filename)


class SignatureAuditDownloadView(StaffRequiredMixin, View):
    """Stream the DocuSeal audit-log PDF for a completed signature request."""

    def get(self, request, pk):
        sr = get_object_or_404(SignatureRequest, pk=pk)
        if not sr.audit_log_file:
            raise Http404("No audit log has been stored for this signature request.")
        return FileResponse(sr.audit_log_file.open("rb"), as_attachment=False, filename="audit-log.pdf")


# --- Xero ------------------------------------------------------------------

class XeroStatusView(StaffRequiredMixin, TemplateView):
    """Shows whether the CRM is connected to Xero + the Connect / Disconnect controls."""

    template_name = "crm/xero_status.html"

    def get_context_data(self, **kwargs):
        from . import xero as xero_helpers
        ctx = super().get_context_data(**kwargs)
        ctx["connection"] = XeroConnection.objects.first()
        ctx["xero_configured"] = xero_helpers.is_configured()
        return ctx


class XeroConnectView(StaffRequiredMixin, View):
    """Kick off the OAuth dance — redirects to Xero's authorize URL with a CSRF state."""

    def get(self, request):
        import secrets as _secrets
        from . import xero as xero_helpers

        if not xero_helpers.is_configured():
            messages.error(
                request,
                "Xero developer credentials aren't set on this environment yet.",
            )
            return redirect("crm:xero_status")

        state = _secrets.token_urlsafe(16)
        request.session["xero_oauth_state"] = state
        redirect_uri = request.build_absolute_uri(reverse("crm:xero_callback"))
        return redirect(xero_helpers.get_authorize_url(redirect_uri, state))


class XeroCallbackView(StaffRequiredMixin, View):
    """Xero redirects back here with a one-shot code — exchange it for tokens
    and store the connected org."""

    def get(self, request):
        from datetime import timedelta
        from . import xero as xero_helpers
        from django.utils import timezone

        code = request.GET.get("code")
        state = request.GET.get("state")
        expected_state = request.session.pop("xero_oauth_state", None)
        if not code or state != expected_state:
            messages.error(request, "Xero authorisation failed (state mismatch).")
            return redirect("crm:xero_status")

        redirect_uri = request.build_absolute_uri(reverse("crm:xero_callback"))
        try:
            tokens = xero_helpers.exchange_code_for_tokens(code, redirect_uri)
            tenants = xero_helpers.list_authorised_tenants(tokens["access_token"])
        except Exception as exc:
            messages.error(request, f"Couldn't talk to Xero: {exc}")
            return redirect("crm:xero_status")

        if not tenants:
            messages.error(request, "Xero didn't return any organisations for this account.")
            return redirect("crm:xero_status")

        tenant = tenants[0]
        XeroConnection.objects.all().delete()  # only one connection at a time
        XeroConnection.objects.create(
            tenant_id=tenant["tenantId"],
            tenant_name=tenant.get("tenantName") or tenant.get("tenantType") or "Xero org",
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=timezone.now() + timedelta(seconds=int(tokens.get("expires_in", 1800))),
            scopes=tokens.get("scope", ""),
            connected_by=request.user,
        )
        messages.success(request, f"Connected to {tenant.get('tenantName')}.")
        return redirect("crm:xero_status")


class XeroDisconnectView(StaffRequiredMixin, View):
    """Drop the stored tokens locally. Doesn't revoke server-side at Xero —
    staff can do that from their Xero developer dashboard."""

    def post(self, request):
        XeroConnection.objects.all().delete()
        messages.success(request, "Xero disconnected.")
        return redirect("crm:xero_status")


class DealRaiseInvoiceView(StaffRequiredMixin, View):
    """Form to raise an ACCREC invoice on a deal, push it to Xero, and stash a
    XeroInvoice mirror locally so staff can deep-link back."""

    template_name = "crm/deal_raise_invoice.html"

    def _initial_for(self, deal):
        amount = deal.commission or 0
        contact_name = deal.organisation.name if deal.organisation else ""
        return {
            "contact_name": contact_name,
            "reference": deal.name,
            "description": f"Commission for {deal.name}",
            "amount": amount,
            "account_code": "200",
            "tax_type": "NONE",
            "due_days": 30,
            "status": "DRAFT",
        }

    def get(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("organisation"), pk=pk)
        from . import xero as xero_helpers
        if xero_helpers.get_active_connection() is None:
            messages.error(request, "Connect to Xero first.")
            return redirect("crm:xero_status")
        form = XeroInvoiceForm(initial=self._initial_for(deal))
        return render(request, self.template_name, {"form": form, "deal": deal})

    def post(self, request, pk):
        from decimal import Decimal as D
        from . import xero as xero_helpers

        deal = get_object_or_404(Deal.objects.select_related("organisation"), pk=pk)
        form = XeroInvoiceForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "deal": deal})

        line = {
            "Description": form.cleaned_data["description"],
            "Quantity": 1,
            "UnitAmount": str(form.cleaned_data["amount"]),
            "AccountCode": form.cleaned_data["account_code"],
            "TaxType": form.cleaned_data["tax_type"] or "NONE",
        }
        try:
            invoice = xero_helpers.create_invoice(
                contact_name=form.cleaned_data["contact_name"],
                line_items=[line],
                reference=form.cleaned_data["reference"],
                invoice_status=form.cleaned_data["status"],
                due_days=form.cleaned_data["due_days"],
            )
        except xero_helpers.XeroError as exc:
            messages.error(request, str(exc))
            return render(request, self.template_name, {"form": form, "deal": deal})

        XeroInvoice.objects.create(
            deal=deal,
            xero_invoice_id=invoice["InvoiceID"],
            xero_invoice_number=invoice.get("InvoiceNumber", ""),
            contact_name=form.cleaned_data["contact_name"],
            online_invoice_url=xero_helpers.online_invoice_url(invoice["InvoiceID"]),
            total=D(str(invoice.get("Total", "0"))),
            status=invoice.get("Status", form.cleaned_data["status"]),
            created_by=request.user,
        )
        messages.success(
            request,
            f"Created Xero invoice {invoice.get('InvoiceNumber', '')} "
            f"({invoice.get('Status', '').lower()}).",
        )
        return redirect(deal.get_absolute_url())


# --- Rates -----------------------------------------------------------------

class RatesView(StaffRequiredMixin, TemplateView):
    """Staff rate lookup: enter a term + amount, see every active band that
    applies, ordered by rate-per-thousand descending."""

    template_name = "crm/rates.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal as D

        ctx = super().get_context_data(**kwargs)
        form = RateLookupForm(self.request.GET or None)
        ctx["form"] = form

        if self.request.GET and form.is_valid():
            term = form.cleaned_data["term_months"]
            amount = form.cleaned_data["amount"]
            bands = (
                RateBand.objects.active()
                .select_related("organisation")
                .filter(term_months=term, min_amount__lte=amount, max_amount__gte=amount)
            )
            # rate_per_thousand is a computed property, so sort in Python (ascending).
            results = sorted(bands, key=lambda b: b.rate_per_thousand)
            for b in results:
                b.monthly = (b.rate_per_thousand * amount / D("1000")).quantize(D("0.01"))
            ctx["results"] = results
            ctx["searched"] = True
            ctx["term"] = term
            ctx["amount"] = amount
        return ctx


class RateUploadView(StaffRequiredMixin, View):
    """Upload a lender's rate sheet CSV and upsert RateBand rows.

    CSV columns: minimum, maximum, then one column per term (12, 24, …). Each
    non-blank term cell becomes a band. Idempotent — keyed on
    (org, term, min, max), so re-uploading refreshes yields, same as the
    load_bnp_rates command.
    """

    template_name = "crm/rate_upload.html"

    def get(self, request):
        return render(request, self.template_name, {"form": RateUploadForm()})

    def post(self, request):
        form = RateUploadForm(request.POST, request.FILES)
        if form.is_valid():
            org = form.cleaned_data["organisation"]
            bands = form.cleaned_data["bands"]
            counts = {"new": 0, "changed": 0, "unchanged": 0}
            with transaction.atomic():
                for lo, hi, term, y in bands:
                    counts[RateBand.record(
                        organisation=org,
                        term_months=term,
                        min_amount=lo,
                        max_amount=hi,
                        yield_percent=y,
                    )] += 1
            messages.success(
                request,
                f"{org.name}: {counts['new']} new, {counts['changed']} changed, "
                f"{counts['unchanged']} unchanged.",
            )
            return redirect("crm:rates")

        # Re-render with the chosen lender preserved in the combobox.
        pk = request.POST.get("organisation")
        org = Organisation.objects.filter(pk=pk).first() if pk else None
        return render(
            request,
            self.template_name,
            {"form": form, "selected_org_id": pk or "", "selected_org_label": org or ""},
        )


class RateBandAddView(StaffRequiredMixin, View):
    """Add a single rate band. Routes through RateBand.record() so manual adds
    get the same history-preserving behaviour as the CSV upload (an existing
    active band for the same key is superseded rather than overwritten)."""

    template_name = "crm/rate_band_form.html"

    def get(self, request):
        return render(request, self.template_name, {"form": RateBandForm()})

    def post(self, request):
        form = RateBandForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            status = RateBand.record(
                organisation=cd["organisation"],
                term_months=cd["term_months"],
                min_amount=cd["min_amount"],
                max_amount=cd["max_amount"],
                yield_percent=cd["yield_percent"],
            )
            messages.success(
                request,
                f"Rate {status} for {cd['organisation'].name}: {cd['term_months']}m, "
                f"£{cd['min_amount']:,}–£{cd['max_amount']:,} @ {cd['yield_percent']}%.",
            )
            return redirect("crm:rates")

        pk = request.POST.get("organisation")
        org = Organisation.objects.filter(pk=pk).first() if pk else None
        return render(
            request,
            self.template_name,
            {"form": form, "selected_org_id": pk or "", "selected_org_label": org or ""},
        )


class QuoteRateOptionsView(StaffRequiredMixin, View):
    """HTMX: <option>s for active rate bands matching ?term= and applicable to
    the quote's finance amount — the ?deal=`s funded amount minus the form's
    ?deposit= and ?balloon=. Drives the rate select on the quote form when the
    term, deposit or balloon changes."""

    @staticmethod
    def _amount_param(request, name) -> Decimal:
        raw = (request.GET.get(name) or "").replace(",", "").strip()
        try:
            return Decimal(raw) if raw else Decimal("0")
        except InvalidOperation:
            return Decimal("0")

    def get(self, request):
        term = request.GET.get("term", "")
        deal_pk = request.GET.get("deal", "")
        rates = []
        if term.isdigit():
            qs = (
                RateBand.objects.active()
                .select_related("organisation")
                .filter(term_months=int(term))
            )
            deal = Deal.objects.filter(pk=deal_pk).first() if deal_pk.isdigit() else None
            amount = deal.funded_amount if deal else None
            if amount is not None:
                amount = amount - self._amount_param(request, "deposit") - self._amount_param(request, "balloon")
                qs = qs.filter(min_amount__lte=amount, max_amount__gte=amount)
            rates = qs.order_by("yield_percent")
        return render(request, "crm/_quote_rate_options.html", {"rates": rates})
