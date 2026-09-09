from decimal import Decimal

from django.db import models

from .base import TimestampedModel
from .deal import Deal
from .organisation import Organisation


class Quote(TimestampedModel):
    """A financing quote against a deal — one deal can have many quotes.

    Each quote is a self-contained financing structure: term, rate, and its
    own deposit/balloon/repayment profile, so alternatives like "£5k deposit
    over 60 months" vs "no deposit over 48" can sit side by side on one deal.

    `term` is always the FULL amortisation horizon — it drives the payment,
    APR, flat-rate and schedule maths. `commitment_months`, when set, is the
    shorter window the rate is committed/priced for ("25-year loan, 5-year
    commitment"): rate-band matching and the deal's maturity key on it via
    `priced_term`, and repayments beyond it are indicative until the review.
    `monthly_payment` is computed on access (a property) from this quote's
    `finance_amount`, the chosen rate band's yield, and the term — never stored,
    so it always reflects the deal's current participations and this quote's
    deposit/balloon.
    """

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="quotes")
    rate = models.ForeignKey(
        "RateBand",
        on_delete=models.PROTECT,
        related_name="quotes",
        null=True,
        help_text="The lender rate band this quote is priced on (Asset "
                  "Finance quotes).",
    )
    lender = models.ForeignKey(
        Organisation,
        on_delete=models.PROTECT,
        related_name="direct_quotes",
        null=True,
        blank=True,
        help_text="For commercial quotes with no rate band — the lender "
                  "quoted directly.",
    )
    interest_rate_percent = models.DecimalField(
        "Interest rate",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Annual interest rate for commercial quotes with no rate "
                  "band, e.g. 9.50 for 9.5% p.a. Drives a mortgage-style "
                  "repayment over the full term.",
    )
    term = models.PositiveSmallIntegerField(help_text="Term in months.")
    commitment_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Months the rate is committed for, when shorter than the "
                  "full term. The rate band is matched on this; repayments "
                  "beyond it are indicative until the rate review. Leave "
                  "blank when the rate runs the full term.",
    )
    commission_percent = models.DecimalField(
        "Commission %",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional. Added to the advance — the customer's monthly payment "
                  "is calculated on the grossed-up amount.",
    )
    # Customer-paid contributions that reduce what's financed. `finance_amount`
    # below = deal.funded_amount - deposit - balloon.
    deposit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    balloon = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    repayment_profile = models.CharField(
        max_length=100, blank=True,
        help_text="Free-text repayment profile (e.g. monthly, quarterly, balloon). "
                  "May be structured later.",
    )

    class Meta:
        ordering = ["deal", "term"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(commitment_months__isnull=True)
                | models.Q(commitment_months__lt=models.F("term")),
                name="quote_commitment_lt_term",
            ),
        ]

    @property
    def lender_org(self):
        """The lender behind this quote: the rate band's organisation for
        priced (Asset Finance) quotes, the direct lender for commercial ones."""
        if self.rate_id:
            return self.rate.organisation
        return self.lender

    @property
    def priced_term(self) -> int:
        """The term the rate applies to — the committed window when set,
        else the full term. Rate-band matching and maturity read this."""
        return self.commitment_months or self.term

    @property
    def yield_percent(self) -> "Decimal | None":
        """The rate band's yield (Asset Finance quotes). Commercial quotes
        carry `interest_rate_percent` instead — a different convention, not
        a yield."""
        return self.rate.yield_percent if self.rate_id else None

    @property
    def finance_amount(self) -> "Decimal | None":
        """The amount this quote finances: the deal's funded amount minus this
        quote's deposit and balloon. None while the deal has no participations."""
        funded = self.deal.funded_amount if self.deal_id else None
        if funded is None:
            return None
        return funded - (self.deposit or Decimal("0")) - (self.balloon or Decimal("0"))

    def __str__(self) -> str:
        rate = f"{self.rate.yield_percent}%" if self.rate_id else "no rate"
        if self.monthly_payment is None:
            return f"{self.term}m @ {rate} — payment TBC"
        return f"{self.term}m @ {rate} — £{self.monthly_payment}/mo"

    def calculate_monthly_payment(self) -> Decimal | None:
        """Monthly payment for this quote. Returns None if any input is
        missing. Math lives in `crm.pricing.monthly_payment` — shared with the
        public quote API so partners get the same numbers as the internal tool."""
        from .. import pricing  # avoid circular import; pricing imports RateBand

        if not (self.deal_id and self.term):
            return None
        if self.rate_id:
            # Asset Finance: the rate band's annuity-due spreadsheet formula.
            return pricing.monthly_payment(
                principal=self.finance_amount,
                rate_band=self.rate,
                term_months=self.term,
                commission_percent=self.commission_percent,
            )
        if self.interest_rate_percent is not None:
            # Commercial: standard mortgage amortisation at the entered rate.
            # Commission is the broker's record, not loaded onto the payment.
            return pricing.commercial_monthly_payment(
                principal=self.finance_amount,
                annual_rate_percent=self.interest_rate_percent,
                term_months=self.term,
            )
        return None

    @property
    def monthly_payment(self) -> "Decimal | None":
        return self.calculate_monthly_payment()

    @property
    def apr(self) -> "Decimal | None":
        from .. import pricing

        if not self.deal_id:
            return None
        return pricing.apr(
            principal=self.finance_amount,
            monthly_payment=self.monthly_payment,
            term_months=self.term,
        )

    @property
    def flat_rate(self) -> "Decimal | None":
        from .. import pricing

        if not self.deal_id:
            return None
        return pricing.flat_rate(
            principal=self.finance_amount,
            monthly_payment=self.monthly_payment,
            term_months=self.term,
        )

