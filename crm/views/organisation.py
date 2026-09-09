from django.db.models import F, Value
from django.db.models.functions import Coalesce, Lower
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..forms import OrganisationForm
from ..models import Deal, Organisation
from .shared import (
    OwnerFilterMixin,
    ProtectedDeleteMixin,
    SearchableListView,
    SectorFilterMixin,
    SortableListMixin,
    _deal_summaries,
    _OwnerFormMixin,
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


class OrganisationSearchView(_SearchView):
    model = Organisation
    search_fields = ["name", "legal_name", "trading_name", "companies_house_number"]
