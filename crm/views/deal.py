from django.contrib import messages
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Coalesce, Lower
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from accounts.permissions import FinanceRequiredMixin, StaffRequiredMixin

from .. import docuseal, pricing, stats
from ..access import get_visible_deal_or_404, visible_contacts, visible_deals
from ..applicants import removal_blocker, remove_applicant
from ..forms import DealForm, XeroInvoiceForm
from ..models import Contact, Deal, Stage, XeroInvoice
from .shared import (
    OwnerFilterMixin,
    SearchableListView,
    SortableListMixin,
    VisibleDealsMixin,
    _attach_activity_labels,
    _deal_summaries,
    _OwnerFormMixin,
)


class DealListView(SortableListMixin, OwnerFilterMixin, StaffRequiredMixin, SearchableListView):
    model = Deal

    def get_base_queryset(self):
        return visible_deals(self.request.user)
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
        source = self.request.GET.get("source", "")
        if source == "none":
            qs = qs.filter(Q(source__isnull=True) | Q(source=""))
        elif source in Deal.Source.values:
            qs = qs.filter(source=source)
        return self.apply_owner_filter(qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _attach_activity_labels(ctx["object_list"])
        ctx["stage_choices"] = Stage.Name.choices
        ctx["stage_filter"] = self.request.GET.get("stage", "")
        ctx["type_choices"] = Deal.Type.choices
        ctx["type_filter"] = self.request.GET.get("type", "")
        ctx["source_choices"] = Deal.Source.choices
        ctx["source_filter"] = self.request.GET.get("source", "")
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
            visible_deals(self.request.user).select_related(
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


class ReportsView(OwnerFilterMixin, StaffRequiredMixin, TemplateView):
    """Deal statistics — pipeline, funded/commission over time, by owner, by
    source, conversion and velocity. All the numbers come from `crm.stats`;
    filters are plain GET params (`period`, `type`, `source`, `owner`) in the
    same vocabulary as the Deals list so rows can deep-link into it."""

    template_name = "crm/reports.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)  # owner_choices / owner_filter
        params = stats.StatsParams.from_request(self.request)
        # The "By owner" league table goes with the owner filter: admins and
        # associates linked to an organisation get it (scoped to the org);
        # an associate with no organisation sees only their own numbers
        # (StatsParams forces owner=me for them).
        ctx.update(stats.report_stats(params, include_owners=not self.owner_filter_locked))
        ctx["period_choices"] = list(stats.PERIODS.items())
        ctx["type_choices"] = Deal.Type.choices
        ctx["type_filter"] = params.type
        ctx["source_choices"] = Deal.Source.choices
        ctx["source_filter"] = params.source
        # Querystring suffix for deep links into the deal list (owner/type/source).
        ctx["list_qs"] = "".join(f"&{k}={v}" for k, v in params.list_query().items())
        return ctx


class DealDetailView(StaffRequiredMixin, DetailView):
    model = Deal

    def get_queryset(self):
        return visible_deals(self.request.user).select_related(
            "owner",
            "customer",
            "organisation",
            "introducer",
        ).prefetch_related(
            "customer__organisations",
            "quotes__rate__organisation",
            "quotes__lender",
            "stage_events",
            "co_applicants",
            "co_applicant_links__contact__user",
            "co_applicant_links__added_by",
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

        # Applicants = the lead (customer, if set) plus any co-applicants. Each
        # co-applicant carries its through row as `.link` so the template can
        # flag people the customer added from the portal (unverified) and
        # offer a remove button; the lead has no link.
        co_applicants = []
        for link in deal.co_applicant_links.all():
            link.contact.link = link
            co_applicants.append(link.contact)
        applicants = [a for a in (deal.customer, *co_applicants) if a]
        ctx["applicants"] = applicants

        # Other contacts in the deal's organisation that this user can see
        # and attach as applicants.
        org = deal.organisation
        ctx["available_applicants"] = (
            visible_contacts(self.request.user)
            .filter(organisations=org)
            .exclude(pk__in=[a.pk for a in applicants])
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
                self.parent_contact = visible_contacts(request.user).get(pk=contact_pk)
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


class DealUpdateView(StaffRequiredMixin, VisibleDealsMixin, _DealFormMixin, UpdateView):
    model = Deal


class DealDeleteView(StaffRequiredMixin, VisibleDealsMixin, DeleteView):
    model = Deal
    success_url = reverse_lazy("crm:deal_list")


class DealApplicantAddView(StaffRequiredMixin, View):
    """Attach an existing contact from the customer's organisation as a co-applicant."""

    def post(self, request, pk):
        deal = get_visible_deal_or_404(request.user, pk, "organisation")
        contact = get_object_or_404(visible_contacts(request.user), pk=request.POST.get("contact"))
        if deal.organisation_id is None:
            messages.error(request, "Set the deal's organisation before adding applicants.")
        elif not contact.organisations.filter(pk=deal.organisation_id).exists():
            messages.error(request, "You can only add applicants from the deal's organisation.")
        elif contact.pk == deal.customer_id:
            messages.error(request, "That contact is already the lead applicant.")
        else:
            deal.co_applicants.add(
                contact,
                through_defaults={"added_by": request.user, "added_at": timezone.now()},
            )
            messages.success(request, f"Added {contact} as an applicant.")
        return redirect(deal.get_absolute_url())


class DealApplicantRemoveView(StaffRequiredMixin, View):
    """Detach a co-applicant from a deal. Refused while they have a signature
    request or an upload on it (see ``crm.applicants.removal_blocker``); the
    Contact itself is never deleted."""

    def post(self, request, pk, contact_pk):
        deal = get_visible_deal_or_404(request.user, pk)
        contact = get_object_or_404(deal.co_applicants, pk=contact_pk)
        blocker = removal_blocker(deal, contact)
        if blocker:
            messages.error(request, blocker)
        else:
            remove_applicant(deal, contact, by_user=request.user, log=False)
            messages.success(request, f"Removed {contact} from the applicants.")
        return redirect(deal.get_absolute_url())


class RequestDealCommissionInvoiceView(StaffRequiredMixin, View):
    """Staff: notify the ACCOUNTS_EMAILS list to raise a commission invoice for
    this deal. Requires an accepted lender (the selected Terms row on a
    commercial deal, the selected Proposal otherwise) so the email names
    the lender."""

    def post(self, request, pk):
        deal = get_visible_deal_or_404(
            request.user, pk,
            "organisation", "customer",
            "selected_proposal", "selected_proposal__lender",
            "selected_quote", "selected_quote__lender",
        )

        terms = deal.accepted_lender_terms
        if terms is None:
            messages.error(
                request,
                "Select the terms you're proceeding with on this deal before requesting "
                "a commission invoice."
                if deal.is_commercial_finance else
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
            lender_org_name=terms.lender.name,
            proposal_number=terms.reference or "—",
            finance_amount_display=finance_display,
            commission_display=commission_display,
            requested_by=request.user.full_name,
        )

        messages.success(request, "Commission invoice request sent to accounts.")
        return redirect(deal.get_absolute_url())


class DealRaiseInvoiceView(FinanceRequiredMixin, View):
    """Form to raise an ACCREC invoice on a deal, push it to Xero, and stash a
    XeroInvoice mirror locally so staff can deep-link back."""

    template_name = "crm/deal_raise_invoice.html"

    def _initial_for(self, deal):
        # The commission invoice goes to the funder — the accepted lender
        # (selected Terms row / selected proposal) — not to the client (same
        # rule as the accounts email flow in RequestDealCommissionInvoiceView).
        terms = deal.accepted_lender_terms
        reference = deal.name
        if terms.reference:
            # The lender's own reference for the deal — helps them reconcile.
            reference = f"{deal.name} · {terms.reference}"
        return {
            "contact_name": terms.lender.name,
            "reference": reference,
            "description": f"Commission for {deal.name}",
            "amount": deal.commission or 0,
            "account_code": "200",
            "tax_type": "NONE",
            "due_days": 30,
            "status": "DRAFT",
        }

    def get(self, request, pk):
        deal = get_visible_deal_or_404(
            request.user, pk, "organisation",
            "selected_proposal", "selected_proposal__lender",
            "selected_quote", "selected_quote__lender",
        )
        from .. import xero as xero_helpers
        if xero_helpers.get_active_connection() is None:
            messages.error(request, "Connect to Xero first.")
            return redirect("crm:xero_status")
        if deal.accepted_lender_terms is None:
            messages.error(
                request,
                "Select the terms you're proceeding with on this deal first — the "
                "commission invoice is addressed to the lender."
                if deal.is_commercial_finance else
                "Select a Proposal on this deal first — the commission invoice "
                "is addressed to the lender.",
            )
            return redirect(deal.get_absolute_url())
        form = XeroInvoiceForm(initial=self._initial_for(deal))
        return render(request, self.template_name, {"form": form, "deal": deal})

    def post(self, request, pk):
        from decimal import Decimal as D
        from .. import xero as xero_helpers

        deal = get_visible_deal_or_404(request.user, pk, "organisation")
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
