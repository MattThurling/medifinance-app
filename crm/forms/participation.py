from django import forms

from ..models import Participation
from .base import DaisyUIFormMixin


class SupplierInvoiceForm(DaisyUIFormMixin, forms.ModelForm):
    """The two-field form a supplier sees when they follow their invoice link."""

    class Meta:
        model = Participation
        fields = ["invoice_number", "invoice"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional on the model (staff may create a Participation before the
        # invoice exists), but required here: the deal page renders the invoice
        # number as the download link, so without one there is nothing to click.
        self.fields["invoice_number"].required = True

    def clean_invoice(self):
        f = self.cleaned_data.get("invoice")
        if not f:
            raise forms.ValidationError("Please attach your invoice PDF.")
        return f


class ParticipationForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: create / edit a Participation (supplier) on a Deal. `deal` is set by the view.

    On Commercial Finance deals a participation is just a funded amount — the
    supplier/invoice fields are dropped entirely so posted values are ignored,
    not merely hidden.
    """

    class Meta:
        model = Participation
        fields = [
            "amount",
            "organisation",
            "description",
            "invoice_number",
            "invoice_contact",
            "invoice",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)
        if deal is not None and deal.is_commercial_finance:
            for field in ("organisation", "invoice_number", "invoice_contact", "invoice"):
                del self.fields[field]
