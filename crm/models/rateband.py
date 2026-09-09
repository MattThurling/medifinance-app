from decimal import Decimal

from django.db import models
from django.utils import timezone

from .base import TimestampedModel
from .organisation import Organisation


class RateBandQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class RateBand(TimestampedModel):
    """A lender's rate for a band of loan amount at a given term.

    Rates are updated periodically. Rather than editing in place, add a new
    band and deactivate the old one: `is_active` + `effective_from` preserve
    the full history and let a withdrawn band be switched off without losing
    the record. The current rate for an amount + term is the latest active
    band whose [min, max] range contains the amount (see `current_for`).
    """

    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.CASCADE,
        related_name="rate_bands",
        help_text="The lender this rate belongs to.",
    )
    term_months = models.PositiveSmallIntegerField(help_text="Term in months, e.g. 60.")
    yield_percent = models.DecimalField(
        "Yield",
        max_digits=5,
        decimal_places=2,
        help_text="Annual yield, e.g. 15.65 for 15.65%.",
    )
    min_amount = models.PositiveIntegerField(
        help_text="Smallest loan this band applies to, e.g. 1000.",
    )
    max_amount = models.PositiveIntegerField(
        help_text="Largest loan this band applies to, e.g. 250000.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to withdraw this band without deleting it.",
    )
    effective_from = models.DateField(
        default=timezone.localdate,
        help_text="Date this rate took effect. On overlap, the latest active band wins.",
    )

    objects = RateBandQuerySet.as_manager()

    class Meta:
        ordering = ["organisation", "term_months", "min_amount"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_amount__gte=models.F("min_amount")),
                name="rateband_max_gte_min",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.organisation.name} · {self.term_months}m · "
            f"£{self.min_amount:,.0f}–£{self.max_amount:,.0f} @ {self.yield_percent}%"
        )

    def rate_per_thousand_exact(self) -> "Decimal | None":
        """Full-precision monthly rental per £1,000 of capital. Equivalent to

            =-PMT(yield/100/12, term, 1000, 0, 1)

        an annuity-due payment (type=1 — paid at the *start* of each period) on
        a £1,000 advance, fv=0:

            RPT = pv · r · (1+r)^n / ((1+r) · ((1+r)^n − 1)),  pv = 1000

        NOT rounded — callers that multiply by a loan amount must round only at
        the end, else the 2dp rounding gets amplified by amount/1000.
        """
        from .. import pricing  # avoid circular import; pricing imports RateBand

        return pricing.rate_per_thousand_exact(self.yield_percent, self.term_months)

    @property
    def rate_per_thousand(self) -> "Decimal":
        """`rate_per_thousand_exact` rounded to 2dp for display."""
        raw = self.rate_per_thousand_exact()
        return Decimal("0.00") if raw is None else raw.quantize(Decimal("0.01"))

    @classmethod
    def current_for(cls, organisation, amount, term_months):
        """The applicable active band for a loan amount + term, or None."""
        return (
            cls.objects.active()
            .filter(
                organisation=organisation,
                term_months=term_months,
                min_amount__lte=amount,
                max_amount__gte=amount,
            )
            .order_by("-effective_from", "-created_at")
            .first()
        )

    @classmethod
    def record(cls, *, organisation, term_months, min_amount, max_amount, yield_percent):
        """Record a rate for an exact band, preserving history. Returns one of
        'new' / 'changed' / 'unchanged'.

        Used for monthly rate-sheet updates: if the current active band for
        this exact (org, term, min, max) already has this yield, nothing
        happens. If the yield differs, the old band is deactivated and a new
        active band is created (so the change is dated and the old rate is
        kept as history). If no band exists yet, one is created.
        """
        current = (
            cls.objects.active()
            .filter(
                organisation=organisation,
                term_months=term_months,
                min_amount=min_amount,
                max_amount=max_amount,
            )
            .order_by("-effective_from", "-created_at")
            .first()
        )
        if current is not None and current.yield_percent == yield_percent:
            return "unchanged"
        if current is not None:
            current.is_active = False
            current.save(update_fields=["is_active"])
        cls.objects.create(
            organisation=organisation,
            term_months=term_months,
            min_amount=min_amount,
            max_amount=max_amount,
            yield_percent=yield_percent,
            is_active=True,
        )
        return "new" if current is None else "changed"

