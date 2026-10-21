from django import forms
from django.db.models import Q

from ..access import visible_contacts
from ..models import Contact, Deal
from .base import DaisyUIFormMixin, OwnerInitialFormMixin


class DealForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Deal
        fields = [
            "name",
            "type",
            "source",
            "customer",
            "organisation",
            "owner",
            "introducer",
            "initial_fee",
            "final_fee",
            "commission",
            "document_fee",
            "non_completion_fee",
            "mf_invoice_number",
            "first_payment_date",
            "term_end_date",
        ]
        labels = {
            "mf_invoice_number": "MF invoice number",
        }
        widgets = {
            "first_payment_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "term_end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, current_user=current_user, **kwargs)
        # The customer must be a contact this user may see. `organisation`
        # and `introducer` stay unscoped on purpose: the organisation picker
        # is open to everyone (see OrganisationSearchView).
        if current_user is not None:
            customers = visible_contacts(current_user)
            if self.instance.pk and self.instance.customer_id:
                # Keep the saved customer valid on edit even if this user
                # can't otherwise see them, so an unrelated edit still saves.
                customers = Contact.objects.filter(
                    Q(pk__in=customers.values("pk")) | Q(pk=self.instance.customer_id)
                )
            self.fields["customer"].queryset = customers
        self.fields["type"].help_text = ""  # the dropdown explains itself
        # Hide retired types from the dropdown, except the one already on the
        # deal being edited (so saving the form doesn't silently change it).
        current = self.instance.type
        blank = [c for c in self.fields["type"].choices if c[0] == ""]
        offered = [
            (code, label)
            for code, label in Deal.Type.choices
            if code not in Deal.RETIRED_TYPES or code == current
        ]
        # Group the dropdown: Asset Finance on its own, then every other type
        # under a "Commercial Finance" heading (they are all variations of it).
        asset = [c for c in offered if c[0] == Deal.Type.ASSET_FINANCE]
        commercial = [c for c in offered if c[0] != Deal.Type.ASSET_FINANCE]
        self.fields["type"].choices = blank + [
            ("Asset Finance", asset),
            ("Commercial Finance", commercial),
        ]

    def clean_type(self):
        # Normalise the cleared select to NULL so "unset" is one state, not two.
        return self.cleaned_data.get("type") or None

    def clean_source(self):
        return self.cleaned_data.get("source") or None
