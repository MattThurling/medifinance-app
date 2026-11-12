from django.contrib import messages
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Lower
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView

from accounts.permissions import StaffRequiredMixin

from .. import org_review
from ..access import visible_contacts, visible_deals, visible_organisations
from ..forms import OrganisationForm, QuickOrganisationForm
from ..models import Organisation, OrganisationChange
from .shared import (
    OwnerFilterMixin,
    ProtectedDeleteMixin,
    SearchableListView,
    SectorFilterMixin,
    SortableListMixin,
    VisibleOrganisationsMixin,
    _deal_summaries,
    _OwnerFormMixin,
    _QuickCreateView,
    _SearchView,
)


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

    def get_base_queryset(self):
        return visible_organisations(self.request.user)

    def get_queryset(self):
        qs = super().get_queryset().select_related("owner")
        qs = self.apply_owner_filter(qs)
        qs = self.apply_sector_filter(qs)
        if self.review_filter:
            # The dashboard's "awaiting review" queue (crm.org_review).
            qs = qs.filter(pk__in=org_review.needs_review(self.request.user).values("pk"))
        return self.apply_sort(qs)

    @property
    def review_filter(self) -> bool:
        return self.request.GET.get("review") == "1"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["review_filter"] = self.review_filter
        return ctx


class OrganisationDetailView(StaffRequiredMixin, DetailView):
    model = Organisation

    def get_queryset(self):
        return visible_organisations(self.request.user).select_related("owner")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["contacts"] = visible_contacts(self.request.user).filter(organisations=self.object)
        ctx["deals"] = _deal_summaries(
            visible_deals(self.request.user)
            .filter(organisation=self.object)
            .select_related("owner", "customer")
        )
        ctx["notes"] = self.object.notes.select_related("author")
        ctx["review"] = org_review.review_context(self.object, self.request.user)
        return ctx


class OrganisationCreateView(StaffRequiredMixin, _OwnerFormMixin, CreateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationQuickCreateView(_QuickCreateView):
    """Inline "+ New" beside an organisation combobox (deal organisation,
    supplier on a participation)."""

    model = Organisation
    form_class = QuickOrganisationForm
    full_create_url_name = "crm:organisation_create"
    quick_title = "New organisation"


class OrganisationUpdateView(StaffRequiredMixin, VisibleOrganisationsMixin, _OwnerFormMixin, UpdateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationDeleteView(
    StaffRequiredMixin, VisibleOrganisationsMixin, ProtectedDeleteMixin, DeleteView
):
    model = Organisation
    success_url = reverse_lazy("crm:organisation_list")
    protected_message = "Can't delete this organisation — it still has contacts linked to it."

    @property
    def protected_redirect_url(self):
        return self.object.get_absolute_url()


class _OrganisationReviewAction(StaffRequiredMixin, View):
    """Base for the review panel's POST-only actions
    (``crm/_organisation_review.html``). Any staff member may act on an
    organisation they can see; everything else 404s. Redirects back to the
    page the panel was on (``next``), else the organisation."""

    def get_organisation(self, pk):
        return get_object_or_404(visible_organisations(self.request.user), pk=pk)

    def back(self, fallback):
        target = self.request.POST.get("next", "")
        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return redirect(target)
        return redirect(fallback)


class OrganisationVerifyView(_OrganisationReviewAction):
    """Mark a customer-created organisation as checked."""

    def post(self, request, pk):
        organisation = self.get_organisation(pk)
        if org_review.verify(organisation, by=request.user):
            messages.success(request, f"{organisation.name} is now verified.")
        else:
            messages.info(request, f"{organisation.name} wasn't waiting to be verified.")
        return self.back(organisation.get_absolute_url())


class OrganisationUseExistingView(_OrganisationReviewAction):
    """Replace a customer-created duplicate with the existing organisation
    it matches. The target comes from the POST but is only accepted if
    ``org_review.likely_duplicates`` offers it (checked in the service)."""

    def post(self, request, pk):
        organisation = self.get_organisation(pk)
        target = Organisation.objects.filter(pk=request.POST.get("target") or 0).first()
        if target is None:
            messages.error(request, "Choose the organisation to use instead.")
            return self.back(organisation.get_absolute_url())
        own_url = organisation.get_absolute_url()
        name = organisation.name
        try:
            org_review.swap_to_existing(organisation, target, by=request.user)
        except org_review.SwapNotAllowed as exc:
            messages.error(request, str(exc))
            return self.back(own_url)
        messages.success(
            request,
            f"“{name}” was replaced by {target.name}. Any contact details the "
            "customer entered that differ are waiting for review here.",
        )
        # The duplicate's own page is gone — never send staff back to it.
        if request.POST.get("next") == own_url:
            return redirect(target.get_absolute_url())
        return self.back(target.get_absolute_url())


class _OrganisationChangeAction(_OrganisationReviewAction):
    done_word = ""

    def act(self, change, by) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def post(self, request, pk):
        change = get_object_or_404(
            OrganisationChange.objects.select_related("organisation").filter(
                organisation__in=visible_organisations(request.user),
            ),
            pk=pk,
        )
        if self.act(change, request.user):
            messages.success(request, f"Change {self.done_word}.")
        else:
            messages.info(request, "That change had already been reviewed.")
        return self.back(change.organisation.get_absolute_url())


class OrganisationChangeApproveView(_OrganisationChangeAction):
    done_word = "approved"

    def act(self, change, by):
        return org_review.approve(change, by=by)


class OrganisationChangeRejectView(_OrganisationChangeAction):
    done_word = "rejected"

    def act(self, change, by):
        return org_review.reject(change, by=by)


class OrganisationSearchView(_SearchView):
    """Organisation combobox. Deliberately unscoped: it also picks lenders,
    suppliers and introducers, which any associate may attach to a deal even
    when a colleague outside their team owns the record. Browsing/opening
    organisations is scoped; picking one isn't."""

    model = Organisation
    search_fields = ["name", "legal_name", "trading_name", "companies_house_number"]
