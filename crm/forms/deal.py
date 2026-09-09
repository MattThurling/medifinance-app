from django import forms

from ..models import Deal
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

    def clean_type(self):
        # Normalise the cleared select to NULL so "unset" is one state, not two.
        return self.cleaned_data.get("type") or None

    def clean_source(self):
        return self.cleaned_data.get("source") or None
