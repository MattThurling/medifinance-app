from decimal import Decimal

from django import forms
from django.forms import modelformset_factory

from .models import Contact, Deal, Document, Organisation, Participation, Proposal, Quote, Stage


class DaisyUIFormMixin:
    """Inject DaisyUI input/select/textarea classes onto every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            existing = w.attrs.get("class", "")
            if isinstance(w, (forms.Select, forms.SelectMultiple)):
                base = "select select-bordered w-full"
            elif isinstance(w, forms.Textarea):
                base = "textarea textarea-bordered w-full"
            elif isinstance(w, forms.CheckboxInput):
                base = "checkbox"
            else:
                base = "input input-bordered w-full"
            w.attrs["class"] = f"{existing} {base}".strip()


class OrganisationForm(DaisyUIFormMixin, forms.ModelForm):
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
        ]


class ContactForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone", "organisations"]

    def selected_organisations(self):
        """Orgs to render as chips on the form. On a bound (POSTed) form we
        rebuild from `data` so an invalid re-post preserves the user's picks;
        otherwise we read the saved instance's M2M, falling back to whatever
        the view supplied as `initial['organisations']` (e.g. when creating
        from an org or deal page)."""
        if self.is_bound and hasattr(self.data, "getlist"):
            ids = [pk for pk in self.data.getlist("organisations") if pk]
            if ids:
                return list(Organisation.objects.filter(pk__in=ids))
            return []
        if self.instance.pk:
            return list(self.instance.organisations.all())
        initial = self.initial.get("organisations") or self.fields["organisations"].initial
        if initial:
            pks = [o.pk if hasattr(o, "pk") else o for o in initial]
            return list(Organisation.objects.filter(pk__in=pks))
        return []


class DealForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Deal
        fields = [
            "name",
            "customer",
            "organisation",
            "owner",
            "introducer",
            "earnings",
            "flat_fee",
            "commission",
            "document_fee",
        ]

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_user is not None and not self.instance.pk:
            self.fields["owner"].initial = current_user


class SupplierInvoiceForm(DaisyUIFormMixin, forms.ModelForm):
    """The two-field form a supplier sees when they follow their invoice link."""

    class Meta:
        model = Participation
        fields = ["invoice_number", "invoice"]

    def clean_invoice(self):
        f = self.cleaned_data.get("invoice")
        if not f:
            raise forms.ValidationError("Please attach your invoice PDF.")
        return f


class ParticipationForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: create / edit a Participation (supplier) on a Deal. `deal` is set by the view."""

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


class QuoteForm(DaisyUIFormMixin, forms.ModelForm):
    """Quote ModelForm. `deal` is set by the view from URL/context.
    `monthly_payment` is auto-calculated by `Quote.save()` — not user-entered."""

    class Meta:
        model = Quote
        fields = ["term", "apr"]


class StageForm(DaisyUIFormMixin, forms.ModelForm):
    """Stage-change ModelForm. `deal`, `set_by` and `occurred_at` are set by the view."""

    class Meta:
        model = Stage
        fields = ["name", "note"]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 3}),
        }


class ProposalForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: create / edit a Proposal on a Deal. `deal` is set by the view."""

    class Meta:
        model = Proposal
        fields = ["lender", "contact", "proposal_number", "status"]


# --- Customer portal forms --------------------------------------------------

class QuoteSelectionForm(forms.Form):
    """Step 1 of the customer application: pick one quote.

    Bypasses DaisyUIFormMixin because we want a richly-rendered radio list,
    not a styled <select>.
    """

    quote = forms.ModelChoiceField(
        queryset=Quote.objects.none(),
        widget=forms.RadioSelect,
        empty_label=None,
        label="",
    )

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quote"].queryset = deal.quotes.all() if deal else Quote.objects.none()


ADDRESS_FIELDS_ORG = [
    "address_line1",
    "address_line2",
    "address_city",
    "address_county",
    "address_postcode",
]

ADDRESS_FIELDS_HOME = [
    "home_address_line1",
    "home_address_line2",
    "home_address_city",
    "home_address_county",
    "home_address_postcode",
]


class CompanyInfoForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Organisation
        fields = ["name"] + ADDRESS_FIELDS_ORG


class CustomerInfoForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "date_of_birth", "phone"] + ADDRESS_FIELDS_HOME
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


class CoApplicantForm(DaisyUIFormMixin, forms.ModelForm):
    """One form per co-applicant in the formset. `organisation` is set by the
    view from the deal's customer's org — not in the form."""

    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone", "date_of_birth"] + ADDRESS_FIELDS_HOME
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


CoApplicantFormSet = modelformset_factory(
    Contact,
    form=CoApplicantForm,
    extra=0,
    can_delete=True,
)


class DocumentRequestForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: request a document on a deal (the file comes later, on upload)."""

    class Meta:
        model = Document
        fields = ["name", "required"]


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


class RateLookupForm(DaisyUIFormMixin, forms.Form):
    """The Rates page lookup — pick a term + amount, see the available bands."""

    term_months = forms.IntegerField(min_value=1, max_value=600, label="Term (months)")
    amount = forms.IntegerField(min_value=1, label="Amount (£)")


class DocumentUploadForm(forms.ModelForm):
    """Upload a file against an existing document request."""

    class Meta:
        model = Document
        fields = ["file"]

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            raise forms.ValidationError("Please choose a file to upload.")
        return f
