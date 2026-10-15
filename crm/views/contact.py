from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Lower
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..access import owner_choices, visible_deals
from ..forms import ContactForm
from ..models import Contact, Organisation
from .shared import (
    OwnerFilterMixin,
    ProtectedDeleteMixin,
    SearchableListView,
    SortableListMixin,
    _deal_summaries,
    _OwnerFormMixin,
    _SearchView,
)


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
        # The address book is shared, but the deals on it are not.
        ctx["deals"] = _deal_summaries(
            visible_deals(self.request.user)
            .filter(customer=self.object)
            .select_related("owner", "organisation")
        )
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
                self.parent_deal = (
                    visible_deals(request.user).select_related("organisation").get(pk=deal_pk)
                )
                self.parent_organisation = self.parent_deal.organisation
            except (ObjectDoesNotExist, ValueError, TypeError):
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


class ContactSearchView(_SearchView):
    model = Contact
    search_fields = ["first_name", "last_name", "email", "organisations__name"]

    def get_queryset(self):
        return Contact.objects.prefetch_related("organisations")


class UserSearchView(_SearchView):
    """Search staff users for the owner combobox — the ones this user may
    assign (all staff for admins, their organisation's staff for associates;
    see ``crm.access.owner_choices``)."""

    search_fields = ["first_name", "last_name", "email"]

    def get_queryset(self):
        return owner_choices(self.request.user)
