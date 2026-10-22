import json
from decimal import Decimal, InvalidOperation

from django import forms
from django.urls import reverse_lazy

from ..models import LenderStatus, Quote, RateBand
from .base import DaisyUIFormMixin


class QuoteForm(DaisyUIFormMixin, forms.ModelForm):
    """Quote ModelForm. `deal` is set by the view from URL/context.
    `monthly_payment` is computed on access (a Quote property) — not user-entered.

    The rate is chosen from the active rate bands that apply to this quote's
    finance amount (the deal's funded amount minus the deposit and balloon
    entered here) and the chosen term. The term is itself a select of just the
    terms that have a band covering the amount; changing the term, deposit or
    balloon refreshes the rate options via HTMX.
    """

    term = forms.TypedChoiceField(coerce=int, empty_value=None, label="Term (months)")

    class Meta:
        model = Quote
        fields = ["term", "rate", "deposit", "balloon", "commission_percent", "repayment_profile"]
        help_texts = {
            "deposit": "Subtracted from the funded amount.",
            "balloon": "Subtracted from the funded amount.",
            "repayment_profile": "e.g. monthly, quarterly, balloon",
        }

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.deal = deal or (self.instance.deal if self.instance and self.instance.pk else None)
        amount = self._amount_in_context()

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

        # Changing the term, deposit or balloon refreshes the rate options
        # (scoped to term + this quote's finance amount).
        attrs = {
            "hx-get": reverse_lazy("crm:quote_rate_options"),
            "hx-target": "#id_rate",
            "hx-swap": "innerHTML",
            "hx-trigger": "change",
            "hx-include": "#id_term, #id_deposit, #id_balloon",
        }
        if self.deal:
            attrs["hx-vals"] = json.dumps({"deal": self.deal.pk})
        for name in ("term", "deposit", "balloon"):
            self.fields[name].widget.attrs.update(attrs)

    def _amount_in_context(self) -> "Decimal | None":
        """The amount rate bands must cover: the deal's funded amount minus the
        deposit and balloon in play — the POSTed values on a bound form, the
        saved ones on edit. Unparseable input counts as 0 (field validation
        reports it separately)."""
        funded = self.deal.funded_amount if self.deal else None
        if funded is None:
            return None

        def value(name: str) -> Decimal:
            if self.is_bound:
                raw = (self.data.get(self.add_prefix(name)) or "").replace(",", "").strip()
                try:
                    return Decimal(raw) if raw else Decimal("0")
                except InvalidOperation:
                    return Decimal("0")
            return getattr(self.instance, name, None) or Decimal("0")

        return funded - value("deposit") - value("balloon")


class TermsForm(DaisyUIFormMixin, forms.ModelForm):
    """The Terms form for every deal type except Asset Finance: one lender's
    position on the deal (lender, status, their reference) plus the terms
    they come back with (rate, term, fixed period, commission).

    A row can be saved as soon as the lender is approached — rate and term
    are only required once the status is Approved. No rate bands, no
    deposit/balloon. The monthly payment is mortgage-style amortisation of
    the deal's funded amount at the entered rate (see
    `Quote.calculate_monthly_payment`)."""

    status = forms.ChoiceField(
        choices=LenderStatus.choices,
        initial=LenderStatus.SUBMITTED,
        label="Status",
        help_text="Submitted as soon as you approach the lender; Approved "
                  "once they come back with terms.",
    )
    term = forms.IntegerField(min_value=1, required=False, label="Term (months)")
    commitment_months = forms.IntegerField(
        min_value=1,
        required=False,
        label="Rate fixed for (months)",
        help_text="Optional — when the rate is only fixed for part of the term "
                  "(e.g. 5 years of a 25-year loan). Blank = fixed for the full term.",
    )

    class Meta:
        model = Quote
        fields = [
            "lender", "status", "lender_reference",
            "interest_rate_percent", "term", "commitment_months", "commission_percent",
        ]
        labels = {
            "interest_rate_percent": "Interest rate (% p.a.)",
            "lender_reference": "Lender reference",
            "commission_percent": "Commission %",
        }
        help_texts = {
            "interest_rate_percent": "e.g. 9.50 for 9.5% p.a. Drives the monthly payment.",
            "lender_reference": "The lender's own reference for this application, "
                                "if they've given one.",
            # The model's help text describes Asset Finance pricing (commission
            # grossed onto the payment); on commercial terms it is a record only.
            "commission_percent": "Medifinance's commission from the lender, as a % of "
                                  "the loan. Recorded for invoicing — not added to the "
                                  "customer's payment.",
        }

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deal = deal or (self.instance.deal if self.instance and self.instance.pk else None)
        self.fields["lender"].required = True
        self.fields["interest_rate_percent"].required = False

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        term = cleaned.get("term")
        commitment = cleaned.get("commitment_months")

        if status == LenderStatus.APPROVED:
            for name in ("interest_rate_percent", "term"):
                if cleaned.get(name) is None and name not in self.errors:
                    self.add_error(
                        name,
                        "Required once the lender has approved — enter the terms "
                        "they came back with.",
                    )
        if commitment and not term:
            self.add_error("commitment_months", "Enter the term first.")
        elif term and commitment and commitment >= term:
            self.add_error(
                "commitment_months",
                "Must be shorter than the term — leave blank if the rate is "
                "fixed for the full term.",
            )
        return cleaned


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
        if deal is None:
            qs = Quote.objects.none()
        elif deal.is_commercial_finance:
            # Customers only ever pick between terms the lender has approved.
            qs = deal.quotes.filter(status=LenderStatus.APPROVED)
        else:
            qs = deal.quotes.all()
        self.fields["quote"].queryset = qs
