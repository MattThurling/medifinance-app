from django import forms
from django.forms import modelformset_factory

from .models import Contact, Deal, Organisation, Quote, Stage


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
        fields = ["name"]


class ContactForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone", "organisation"]


class DealForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Deal
        fields = [
            "name",
            "customer",
            "owner",
            "introducer",
            "equipment_supplier",
            "funded_amount",
            "earnings",
            "flat_fee",
            "commission",
            "document_fee",
        ]

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_user is not None and not self.instance.pk:
            self.fields["owner"].initial = current_user


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
        fields = ADDRESS_FIELDS_ORG


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
