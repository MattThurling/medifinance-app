from django import forms

from ..models import Organisation
from .base import (
    ADDRESS_FIELDS_ORG,
    DaisyUIFormMixin,
    OptimisticLockFormMixin,
    OwnerInitialFormMixin,
)


class OrganisationForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Organisation
        fields = [
            "name",
            "legal_name",
            "trading_name",
            "companies_house_number",
            "address_line1",
            "address_line2",
            "address_city",
            "address_county",
            "address_postcode",
            "url",
            "email",
            "phone",
            "sector",
            "owner",
        ]
        labels = {"url": "Website"}

    def clean_sector(self):
        # Normalise the cleared select to NULL so "unset" is one state, not two.
        return self.cleaned_data.get("sector") or None


class CustomerCompanyForm(OptimisticLockFormMixin, DaisyUIFormMixin, forms.ModelForm):
    """Customer portal: the organisation fields a customer may edit inline.

    Name and Companies House number are deliberately absent — locked for
    customers (a rename ripples across the CRM); ModelForms ignore posted
    fields not in Meta.fields, so omission IS the lock."""

    class Meta:
        model = Organisation
        fields = ADDRESS_FIELDS_ORG + ["phone", "email", "url"]
        labels = {"url": "Website"}
