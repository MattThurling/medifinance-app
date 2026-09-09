from decimal import Decimal

from django import forms

from .base import DaisyUIFormMixin


class XeroInvoiceForm(DaisyUIFormMixin, forms.Form):
    """The Raise-Invoice form. Defaults are filled from the deal by the view."""

    STATUS_CHOICES = (
        ("DRAFT", "Draft (review + send from Xero)"),
        ("AUTHORISED", "Authorised (ready to send)"),
    )

    contact_name = forms.CharField(
        max_length=255,
        help_text="Who the invoice is billed to in Xero. Created if it doesn't exist yet.",
    )
    reference = forms.CharField(max_length=255, required=False, help_text="Your reference for this invoice.")
    description = forms.CharField(max_length=500)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    account_code = forms.CharField(
        max_length=10,
        help_text="Your Xero revenue account code, e.g. 200.",
    )
    tax_type = forms.CharField(
        max_length=20, required=False, initial="NONE",
        help_text="Xero tax type code. Leave as NONE for non-VAT line items.",
    )
    due_days = forms.IntegerField(min_value=0, max_value=365, initial=30)
    status = forms.ChoiceField(choices=STATUS_CHOICES, initial="DRAFT")
