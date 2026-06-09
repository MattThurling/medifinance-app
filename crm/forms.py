import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.forms import modelformset_factory
from django.urls import reverse_lazy

from .models import Contact, Deal, Document, Organisation, Participation, Proposal, Quote, RateBand, Stage


def _parse_amount(raw: str) -> "int | None":
    """'£14,999' / '1000' -> int; blank/non-numeric -> None."""
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def _parse_yield(raw: str) -> "Decimal | None":
    """'15.65%' / '7.75' -> Decimal(2dp); blank/non-numeric/out-of-range -> None."""
    s = (raw or "").replace("%", "").replace(",", "").strip()
    if not s:
        return None
    try:
        value = Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    # yield_percent is DecimalField(max_digits=5, decimal_places=2) -> < 1000.
    if value < 0 or value >= 1000:
        return None
    return value


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
    `monthly_payment` is computed on access (a Quote property) — not user-entered.

    The rate is chosen from the active rate bands that apply to this deal's
    funded amount and the chosen term. The term is itself a select of just the
    terms that have a band covering the amount; picking one refreshes the rate
    options via HTMX.
    """

    term = forms.TypedChoiceField(coerce=int, empty_value=None, label="Term (months)")

    class Meta:
        model = Quote
        fields = ["term", "rate", "commission_percent"]
        help_texts = {"commission_percent": "Optional."}

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.deal = deal or (self.instance.deal if self.instance and self.instance.pk else None)
        amount = self.deal.funded_amount if self.deal else None

        # Bands that apply to this deal's amount (the pool both selects draw on).
        pool = RateBand.objects.active().select_related("organisation")
        if amount is not None:
            pool = pool.filter(min_amount__lte=amount, max_amount__gte=amount)

        # Term select = the distinct terms in that pool. Keep the current term
        # selectable on edit even if no longer offered.
        terms = sorted(set(pool.values_list("term_months", flat=True)))
        if self.instance and self.instance.pk and self.instance.term and self.instance.term not in terms:
            terms = sorted(set(terms) | {self.instance.term})
        self.fields["term"].choices = [("", "Select a term…")] + [(t, f"{t} months") for t in terms]

        # Rate options are the pool narrowed to the in-context term (submitted on
        # POST, or the instance's on edit). Unknown term -> empty until picked.
        term_val = None
        if self.is_bound:
            term_val = self.data.get(self.add_prefix("term"))
        elif self.instance and self.instance.pk:
            term_val = self.instance.term

        rate_qs = pool.filter(term_months=int(term_val)) if term_val and str(term_val).isdigit() else pool.none()
        rate = self.fields["rate"]
        rate.queryset = rate_qs.order_by("yield_percent")
        rate.label = "Rate"
        rate.empty_label = "Select a rate…"
        rate.label_from_instance = lambda rb: f"{rb.organisation.name} — {rb.yield_percent}%"

        # Changing the term refreshes the rate options (scoped to term + amount).
        attrs = {
            "hx-get": reverse_lazy("crm:quote_rate_options"),
            "hx-target": "#id_rate",
            "hx-swap": "innerHTML",
            "hx-trigger": "change",
        }
        if self.deal:
            attrs["hx-vals"] = json.dumps({"deal": self.deal.pk})
        self.fields["term"].widget.attrs.update(attrs)


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


class RateBandForm(DaisyUIFormMixin, forms.ModelForm):
    """Add a single rate band manually. `organisation` is rendered as a search
    combobox (not the default select), so DaisyUI select styling on it is moot."""

    class Meta:
        model = RateBand
        fields = ["organisation", "term_months", "min_amount", "max_amount", "yield_percent"]
        help_texts = {f: "" for f in ("term_months", "min_amount", "max_amount", "yield_percent")}

    def clean(self):
        cleaned = super().clean()
        lo = cleaned.get("min_amount")
        hi = cleaned.get("max_amount")
        if lo is not None and hi is not None and hi < lo:
            self.add_error("max_amount", "Maximum must be greater than or equal to minimum.")
        return cleaned


class RateUploadForm(forms.Form):
    """Upload a lender's rate sheet as CSV.

    Required columns: `minimum`, `maximum`, plus one column per term whose
    header is the term in months (e.g. 12, 24, 36…). Each non-blank term cell
    in a row becomes a RateBand for that (amount band × term). `clean()` parses
    the file and stashes the parsed bands in `cleaned_data['bands']` as a list
    of (min, max, term, yield) tuples for the view to upsert.
    """

    organisation = forms.ModelChoiceField(
        queryset=Organisation.objects.all(),
        error_messages={"required": "Pick the lender these rates belong to."},
    )
    file = forms.FileField()

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not (f.name or "").lower().endswith(".csv"):
            raise forms.ValidationError("Please upload a .csv file.")
        return f

    def clean(self):
        cleaned = super().clean()
        f = cleaned.get("file")
        if not f:
            return cleaned  # a clean_file error already covers this

        try:
            f.seek(0)
            text = f.read().decode("utf-8-sig")
        except (UnicodeDecodeError, OSError):
            raise forms.ValidationError("Couldn't read the file as UTF-8 CSV.")

        reader = csv.DictReader(io.StringIO(text))
        headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        lower = {h.lower(): h for h in headers}
        if "minimum" not in lower or "maximum" not in lower:
            raise forms.ValidationError("CSV must have 'minimum' and 'maximum' columns.")
        term_cols = [(int(h), h) for h in headers if h.isdigit()]
        if not term_cols:
            raise forms.ValidationError("CSV must have at least one term column (e.g. 12, 24, 36).")

        bands: list[tuple[int, int, int, Decimal]] = []
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            lo = _parse_amount(row.get(lower["minimum"]))
            hi = _parse_amount(row.get(lower["maximum"]))
            if lo is None or hi is None:
                raise forms.ValidationError(f"Row {i}: minimum and maximum must both be amounts.")
            if hi < lo:
                raise forms.ValidationError(f"Row {i}: maximum must be greater than or equal to minimum.")
            for term, header in term_cols:
                cell = (row.get(header) or "").strip()
                if not cell:
                    continue
                y = _parse_yield(cell)
                if y is None:
                    raise forms.ValidationError(f"Row {i}: '{cell}' in column '{header}' isn't a valid rate.")
                bands.append((lo, hi, term, y))

        if not bands:
            raise forms.ValidationError("No rates found in the file.")
        cleaned["bands"] = bands
        return cleaned


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
