from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def _hubspot_url(object_type: str, hubspot_id: str | None) -> str | None:
    """Build a deep-link to a record in HubSpot, or None if there's no hubspot_id.

    `object_type` is HubSpot's CRM object code: 0-1 contact, 0-2 company, 0-3 deal.
    """
    if not hubspot_id:
        return None
    portal = settings.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{portal}/record/{object_type}/{hubspot_id}"


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organisation(TimestampedModel):
    name = models.CharField(max_length=255)

    # UK structured address. All blank-allowed — fill in as you go.
    address_line1 = models.CharField("Line 1", max_length=255, blank=True, help_text="House number and street")
    address_line2 = models.CharField("Line 2", max_length=255, blank=True, help_text="Flat, suite, etc. (optional)")
    address_city = models.CharField("City", max_length=100, blank=True)
    address_county = models.CharField("County", max_length=100, blank=True, help_text="Optional")
    address_postcode = models.CharField("Postcode", max_length=10, blank=True)

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("crm:organisation_detail", args=[self.pk])

    @property
    def hubspot_url(self) -> str | None:
        return _hubspot_url("0-2", self.hubspot_id)


class Contact(TimestampedModel):
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    # UK structured home address.
    home_address_line1 = models.CharField("Line 1", max_length=255, blank=True, help_text="House number and street")
    home_address_line2 = models.CharField("Line 2", max_length=255, blank=True, help_text="Flat, etc. (optional)")
    home_address_city = models.CharField("City", max_length=100, blank=True)
    home_address_county = models.CharField("County", max_length=100, blank=True, help_text="Optional")
    home_address_postcode = models.CharField("Postcode", max_length=10, blank=True)

    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.PROTECT,
        related_name="contacts",
    )

    # Optional link back to a User account, only set if this contact has a customer login.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact",
    )

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.email or f"Contact #{self.pk}"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def get_absolute_url(self) -> str:
        return reverse("crm:contact_detail", args=[self.pk])

    @property
    def hubspot_url(self) -> str | None:
        return _hubspot_url("0-1", self.hubspot_id)


class Deal(TimestampedModel):
    name = models.CharField(max_length=255)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_deals",
    )
    customer = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name="deals",
    )

    # Optional associations
    introducer = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        related_name="introduced_deals",
        null=True,
        blank=True,
    )
    equipment_supplier = models.ForeignKey(
        Organisation,
        on_delete=models.SET_NULL,
        related_name="supplied_deals",
        null=True,
        blank=True,
    )

    # Financials (all nullable — fill in as the deal progresses)
    funded_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    earnings = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    flat_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    commission = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    document_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Customer's chosen quote, picked via the portal application
    selected_quote = models.ForeignKey(
        "Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # People applying alongside the primary customer (e.g. partners, directors)
    co_applicants = models.ManyToManyField(
        Contact,
        blank=True,
        related_name="co_applicant_deals",
    )

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def organisation(self) -> Organisation:
        """Organisation is reached through the customer contact — data consistency by design."""
        return self.customer.organisation

    def get_absolute_url(self) -> str:
        return reverse("crm:deal_detail", args=[self.pk])

    @property
    def hubspot_url(self) -> str | None:
        return _hubspot_url("0-3", self.hubspot_id)

    @property
    def current_stage(self) -> "Stage | None":
        """Latest stage event for this deal (None if there are no events yet)."""
        return self.stage_events.first()


class Stage(TimestampedModel):
    """A stage-change event on a Deal. The latest one is the deal's current stage."""

    class Name(models.TextChoices):
        APPLICATION = "application", "Application"
        INFO_RECEIVED = "info_received", "Info Received"

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="stage_events")
    name = models.CharField(max_length=32, choices=Name.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stage_changes",
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.deal} → {self.get_name_display()} ({self.occurred_at:%d %b %Y})"


class Quote(TimestampedModel):
    """A financing quote against a deal — one deal can have many quotes.

    `monthly_payment` is auto-calculated in `save()` from the deal's
    `funded_amount`, this quote's APR, and the term. Editing APR or term
    recomputes on the next save.
    """

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="quotes")
    apr = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="e.g. 5.99 for 5.99%.",
    )
    term = models.PositiveSmallIntegerField(help_text="Term in months.")
    monthly_payment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Auto-calculated from deal funded amount, APR and term.",
    )

    class Meta:
        ordering = ["deal", "term"]

    def __str__(self) -> str:
        if self.monthly_payment is None:
            return f"{self.term}m @ {self.apr}% — payment TBC"
        return f"{self.term}m @ {self.apr}% — £{self.monthly_payment}/mo"

    def calculate_monthly_payment(self) -> Decimal | None:
        """Standard amortising loan payment.

            M = P · r · (1+r)^n / ((1+r)^n − 1)

        Returns None if any input is missing (e.g. the deal has no funded_amount yet).
        """
        if not (self.deal_id and self.apr is not None and self.term):
            return None
        principal = self.deal.funded_amount
        if principal is None:
            return None

        r = (Decimal(self.apr) / Decimal("100")) / Decimal("12")
        n = int(self.term)
        P = Decimal(principal)

        if r == 0:
            return (P / Decimal(n)).quantize(Decimal("0.01"))

        growth = (Decimal("1") + r) ** n
        return (P * r * growth / (growth - Decimal("1"))).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        computed = self.calculate_monthly_payment()
        if computed is not None:
            self.monthly_payment = computed
        super().save(*args, **kwargs)
