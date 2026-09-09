import csv
import io
from decimal import Decimal

from django import forms

from ..models import Organisation, RateBand
from .base import DaisyUIFormMixin, _parse_amount, _parse_yield


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
