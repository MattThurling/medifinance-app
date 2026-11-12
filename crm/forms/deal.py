from django import forms
from django.db.models import Q

from ..access import visible_contacts
from ..models import Contact, Deal, Organisation
from .base import DaisyUIFormMixin, OwnerInitialFormMixin


class DealForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Deal
        fields = [
            "name",
            "type",
            "source",
            "source_identifier",
            "customer",
            "organisation",
            "owner",
            "introducer",
            "initial_fee",
            "final_fee",
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

    def clean_organisation(self):
        """Don't let a stale form blank an organisation it never showed.

        A portal customer can create the deal's organisation while a staff
        member has this form open (rendered with none). Saving that form
        would otherwise set it back to NULL and orphan the new record."""
        organisation = self.cleaned_data.get("organisation")
        if organisation is None and self.instance.pk and self.initial.get("organisation") is None:
            current = Deal.objects.filter(pk=self.instance.pk).values_list(
                "organisation", flat=True,
            ).first()
            if current is not None:
                return Organisation.objects.get(pk=current)
        return organisation

    def clean_type(self):
        # Normalise the cleared select to NULL so "unset" is one state, not two.
        return self.cleaned_data.get("type") or None

    def clean_source(self):
        return self.cleaned_data.get("source") or None
