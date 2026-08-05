import secrets
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

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
    class Sector(models.TextChoices):
        DENTAL = "dental", "Dental"
        DENTAL_LAB = "dental_lab", "Dental Lab"
        DOCTORS = "doctors", "Doctors"
        PHARMACY = "pharmacy", "Pharmacy"
        VETERINARY = "veterinary", "Veterinary"
        PHYSIO_CHIRO = "physio_chiro", "Physio/Chiro"
        COSMETIC = "cosmetic", "Cosmetic"
        OPTICAL = "optical", "Optical"
        HEARING = "hearing", "Hearing"
        HAIR_CLINIC = "hair_clinic", "Hair Clinic"
        EDUCATION = "education", "Education"
        NON_HEALTHCARE = "non_healthcare", "Non-healthcare profession"
        OTHER = "other", "Other"

    name = models.CharField(
        max_length=255,
        help_text="The everyday name staff use to find this org.",
    )
    legal_name = models.CharField(
        max_length=255, blank=True,
        help_text="Registered Companies House name (used on contracts and credit checks).",
    )
    trading_name = models.CharField(
        max_length=255, blank=True,
        help_text="“Trading as” name, when different from the legal name.",
    )
    companies_house_number = models.CharField(
        "Companies House number",
        max_length=10, blank=True, db_index=True,
        help_text="UK CH number — 8 digits, or NI/SC/OC + 6 digits (LLPs).",
    )

    # UK structured address. All blank-allowed — fill in as you go.
    address_line1 = models.CharField("Line 1", max_length=255, blank=True, help_text="House number and street")
    address_line2 = models.CharField("Line 2", max_length=255, blank=True, help_text="Flat, suite, etc.")
    address_city = models.CharField("City", max_length=100, blank=True)
    address_county = models.CharField("County", max_length=100, blank=True, help_text="Optional")
    address_postcode = models.CharField("Postcode", max_length=10, blank=True)

    url = models.URLField(max_length=255, blank=True, help_text="Homepage / website URL.")
    email = models.EmailField(
        blank=True,
        help_text="Shared / role inbox for the organisation (e.g. info@, reception@). "
                  "Contacts may share this address.",
    )
    phone = models.CharField(max_length=32, blank=True, help_text="Main phone number for the organisation.")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organisations",
        null=True,
        blank=True,
        help_text="Staff member responsible for this organisation. Optional.",
    )

    sector = models.CharField(
        max_length=32,
        choices=Sector.choices,
        null=True,
        blank=True,
        help_text="The profession/sector this organisation operates in. Optional.",
    )

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

    @property
    def display_address(self) -> str:
        """Newline-joined non-empty address lines, suitable for email/PDF bodies."""
        parts = [
            self.address_line1,
            self.address_line2,
            self.address_city,
            self.address_county,
            self.address_postcode,
        ]
        return "\n".join(p for p in parts if p)

    @property
    def companies_house_url(self) -> str | None:
        """Deep-link to this org's Companies House record, or None if no CH number."""
        if not self.companies_house_number:
            return None
        return (
            "https://find-and-update.company-information.service.gov.uk/company/"
            + self.companies_house_number.strip().upper()
        )


class Contact(TimestampedModel):
    title = models.CharField(max_length=20, blank=True, help_text="Honorific (Dr, Prof, Mrs …). Optional.")
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    # UK structured home address.
    home_address_line1 = models.CharField("Line 1", max_length=255, blank=True, help_text="House number and street")
    home_address_line2 = models.CharField("Line 2", max_length=255, blank=True, help_text="Flat, etc.")
    home_address_city = models.CharField("City", max_length=100, blank=True)
    home_address_county = models.CharField("County", max_length=100, blank=True, help_text="Optional")
    home_address_postcode = models.CharField("Postcode", max_length=10, blank=True)

    organisations = models.ManyToManyField(
        Organisation,
        related_name="contacts",
        blank=True,
        help_text="A contact can belong to more than one organisation.",
    )

    # Optional link back to a User account, only set if this contact has a customer login.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_contacts",
        null=True,
        blank=True,
        help_text="Staff member responsible for this contact — not the contact's "
                  "own portal login (that's `user`).",
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
    class Type(models.TextChoices):
        ASSET_FINANCE = "asset_finance", "Asset Finance"
        COMMERCIAL_FINANCE = "commercial_finance", "Commercial Finance"

    name = models.CharField(max_length=255)
    type = models.CharField(
        max_length=32,
        choices=Type.choices,
        null=True,
        blank=True,
        help_text="The kind of finance this deal is for. Optional.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_deals",
        null=True,
        help_text="Deals created via the public API start unowned.",
    )
    customer = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name="deals",
        null=True,
        blank=True,
        help_text="Nullable — some commercial / historical deals genuinely have "
                  "no individual customer, only an organisation.",
    )
    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.PROTECT,
        related_name="deals",
        null=True,
        blank=True,
        help_text="The organisation this deal is for. May be different from any "
                  "of the customer's organisations; can be set later.",
    )

    # Optional associations
    introducer = models.ForeignKey(
        Organisation,
        on_delete=models.SET_NULL,
        related_name="introduced_deals",
        null=True,
        blank=True,
    )
    # Other financials (all nullable). `funded_amount` is derived as the sum of
    # this deal's Participations — see the property below.
    earnings = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    flat_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    commission = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    document_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Medifinance's own commission invoice to the funder. A deal is only
    # considered mf_invoiced when this is set.
    mf_invoice_number = models.CharField(max_length=32, blank=True)

    first_payment_date = models.DateField(
        null=True, blank=True,
        help_text="Date the first repayment falls due.",
    )
    term_end_date = models.DateField(
        null=True, blank=True,
        help_text="When the finance term ends. Migrated from HubSpot for "
                  "historical deals; leave blank to derive it from the first "
                  "payment date + selected quote's term.",
    )

    # Customer's chosen quote, picked via the portal application
    selected_quote = models.ForeignKey(
        "Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The proposal staff has chosen to run with (typically the approved one).
    selected_proposal = models.ForeignKey(
        "Proposal",
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

    created_via_api_key = models.ForeignKey(
        "accounts.ApiKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_deals",
        help_text="The partner API key that POSTed this deal in, if any. "
                  "Used to attribute and rate-limit API-created deals.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("crm:deal_detail", args=[self.pk])

    @property
    def is_commercial_finance(self) -> bool:
        return self.type == Deal.Type.COMMERCIAL_FINANCE

    @property
    def hubspot_url(self) -> str | None:
        return _hubspot_url("0-3", self.hubspot_id)

    @property
    def funded_amount(self) -> "Decimal | None":
        """Sum of all participation amounts. Returns None when no participations
        exist yet (preserves the pre-Participation `funded_amount is None`
        semantic). Uses `participations.all()` so a prefetched queryset is cheap."""
        ps = list(self.participations.all())
        if not ps:
            return None
        return sum((p.amount for p in ps), Decimal("0"))

    @property
    def finance_amount(self) -> "Decimal | None":
        """The amount actually being financed: the funded amount minus the
        selected quote's deposit and balloon. Falls back to the plain funded
        amount while no quote is selected; None when there are no
        participations yet."""
        quote = self.selected_quote
        if quote is not None:
            return quote.finance_amount
        return self.funded_amount

    @property
    def current_stage(self) -> "Stage | None":
        """Latest stage event for this deal (None if there are no events yet)."""
        return self.stage_events.first()

    @property
    def maturity_date(self) -> "date | None":
        """When this deal's finance term ends. The explicit `term_end_date`
        (set by staff or the HubSpot migration) wins; otherwise derived as the
        selected quote's final payment date. None when neither is available."""
        if self.term_end_date:
            return self.term_end_date
        quote = self.selected_quote
        if self.first_payment_date is None or quote is None:
            return None
        from . import pricing  # avoid circular import; pricing imports RateBand

        return pricing.add_months(self.first_payment_date, quote.term - 1)

    @property
    def repayment_schedule(self) -> "list[dict] | None":
        """Amortisation schedule for the selected quote, starting at
        `first_payment_date`. None until both are set (and the quote's monthly
        payment is computable) — see `crm.pricing.repayment_schedule`."""
        from . import pricing  # avoid circular import; pricing imports RateBand

        quote = self.selected_quote
        if self.first_payment_date is None or quote is None:
            return None
        return pricing.repayment_schedule(
            principal=quote.finance_amount,
            monthly_payment=quote.monthly_payment,
            term_months=quote.term,
            first_payment_date=self.first_payment_date,
        )


def participation_invoice_upload_path(instance: "Participation", filename: str) -> str:
    return f"deals/{instance.deal_id}/invoices/{filename}"


class Participation(TimestampedModel):
    """One contribution to a deal's funded amount. Many participations per deal;
    the sum of their `amount` is the deal's funded amount.

    Most fields are optional — staff often start with just an amount and fill in
    the supplier, invoice and description as the deal progresses.
    """

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="participations")
    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.PROTECT,
        related_name="participations",
        null=True,
        blank=True,
        help_text="The supplier this amount goes to. Optional — may be added later.",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    description = models.TextField(
        blank=True,
        help_text="What this participation covers (e.g. equipment, install fees).",
    )
    invoice_number = models.CharField(
        max_length=100, blank=True,
        help_text="Supplier's invoice / reference number for this amount.",
    )
    invoice_contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        related_name="invoiced_participations",
        null=True, blank=True,
        help_text="The supplier-side contact who handles invoicing.",
    )
    invoice = models.FileField(
        upload_to=participation_invoice_upload_path,
        null=True, blank=True,
        help_text="The supplier's invoice (PDF).",
    )

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        org = self.organisation.name if self.organisation else "TBD"
        return f"{org} — £{self.amount:,.2f}"


class ParticipationInvoiceLink(TimestampedModel):
    """A single-use, time-limited link sent to a supplier so they can upload
    their invoice for one Participation. No login required — the token is the
    only auth, same UX as the customer MagicLink but pointing at the upload form.
    """

    DEFAULT_TTL_DAYS = 7

    participation = models.ForeignKey(
        Participation, on_delete=models.CASCADE, related_name="invoice_links"
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="issued_invoice_links",
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        status = "used" if self.used_at else ("expired" if self.is_expired else "active")
        return f"InvoiceLink({self.participation_id}, {status})"

    @classmethod
    def issue(cls, *, participation: "Participation", created_by=None,
              ttl_days: int = DEFAULT_TTL_DAYS) -> "ParticipationInvoiceLink":
        return cls.objects.create(
            participation=participation,
            token=secrets.token_urlsafe(32),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )

    @property
    def is_consumed(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_consumed and not self.is_expired

    def consume(self, *, ip: str | None = None) -> None:
        self.used_at = timezone.now()
        self.used_ip = ip
        self.save(update_fields=["used_at", "used_ip"])


class Proposal(TimestampedModel):
    """A proposal sent to a lender for a deal. A broker typically 'shops' a
    deal to multiple lenders simultaneously, so each Deal can have many
    Proposals."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="proposals")
    lender = models.ForeignKey(
        Organisation,
        on_delete=models.PROTECT,
        related_name="lender_proposals",
        help_text="The lender we sent this proposal to.",
    )
    contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        related_name="proposals",
        null=True, blank=True,
        help_text="The contact at the lender, if known.",
    )
    proposal_number = models.CharField(
        max_length=100, blank=True,
        help_text="The lender's reference for this proposal.",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SUBMITTED,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.lender.name} ({self.get_status_display()})"


class Stage(TimestampedModel):
    """A stage-change event on a Deal. The latest one is the deal's current stage."""

    class Name(models.TextChoices):
        APPLICATION = "application", "Application"
        INFO_RECEIVED = "info_received", "Info Received"
        PROPOSAL_SUBMITTED = "proposal_submitted", "Proposal Submitted"
        PROPOSAL_APPROVED = "proposal_approved", "Proposal Approved"
        PROPOSAL_DECLINED = "proposal_declined", "Proposal Declined"
        DOCUMENTS_OUT = "documents_out", "Documents Out"
        SUPPLIER_INVOICE_REQUESTED = "supplier_invoice_requested", "Supplier Invoice Requested"
        SUPPLIER_INVOICE_RECEIVED = "supplier_invoice_received", "Supplier Invoice Received"
        MF_INVOICED = "mf_invoiced", "MF Invoiced"
        DEAL_LIVE = "deal_live", "Deal Live"
        DORMANT = "dormant", "Dormant"
        LOST = "lost", "Lost"
        SETTLED = "settled", "Settled"

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="stage_events")
    name = models.CharField(max_length=32, choices=Name.choices)
    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Which organisation this stage is about — the client, the "
                  "lender (for proposal stages), or the supplier (for invoice stages).",
    )
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
        indexes = [models.Index(fields=["deal", "-occurred_at"], name="crm_stage_deal_occurred")]

    def __str__(self) -> str:
        return f"{self.deal} → {self.get_name_display()} ({self.occurred_at:%d %b %Y})"


class Quote(TimestampedModel):
    """A financing quote against a deal — one deal can have many quotes.

    Each quote is a self-contained financing structure: term, rate, and its
    own deposit/balloon/repayment profile, so alternatives like "£5k deposit
    over 60 months" vs "no deposit over 48" can sit side by side on one deal.
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
        help_text="The lender rate band this quote is priced on.",
    )
    term = models.PositiveSmallIntegerField(help_text="Term in months.")
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

    @property
    def yield_percent(self) -> "Decimal | None":
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
        from . import pricing  # avoid circular import; pricing imports RateBand

        if not (self.deal_id and self.rate_id and self.term):
            return None
        return pricing.monthly_payment(
            principal=self.finance_amount,
            rate_band=self.rate,
            term_months=self.term,
            commission_percent=self.commission_percent,
        )

    @property
    def monthly_payment(self) -> "Decimal | None":
        return self.calculate_monthly_payment()

    @property
    def apr(self) -> "Decimal | None":
        from . import pricing

        if not self.deal_id:
            return None
        return pricing.apr(
            principal=self.finance_amount,
            monthly_payment=self.monthly_payment,
            term_months=self.term,
        )

    @property
    def flat_rate(self) -> "Decimal | None":
        from . import pricing

        if not self.deal_id:
            return None
        return pricing.flat_rate(
            principal=self.finance_amount,
            monthly_payment=self.monthly_payment,
            term_months=self.term,
        )


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
        if not self.term_months or self.yield_percent is None:
            return None
        r = (Decimal(self.yield_percent) / Decimal("100")) / Decimal("12")
        n = int(self.term_months)
        pv = Decimal("1000")
        if r == 0:
            return pv / Decimal(n)
        growth = (Decimal("1") + r) ** n
        return pv * r * growth / ((Decimal("1") + r) * (growth - Decimal("1")))

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


class XeroConnection(TimestampedModel):
    """The single Xero organisation this CRM is connected to. Holds the OAuth
    tokens — we only need one row (staff connects once per environment)."""

    tenant_id = models.CharField(max_length=64, unique=True)
    tenant_name = models.CharField(max_length=255)
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField(help_text="When the access token stops working (refreshed automatically).")
    scopes = models.TextField(blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="xero_connections",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Xero · {self.tenant_name}"


class XeroInvoice(TimestampedModel):
    """Local mirror of a Xero invoice we raised against a Deal — so we don't
    re-issue and so staff can jump straight back into Xero."""

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="xero_invoices")
    xero_invoice_id = models.CharField(max_length=64, unique=True)
    xero_invoice_number = models.CharField(max_length=100, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    online_invoice_url = models.URLField(blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=32, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="raised_xero_invoices",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.xero_invoice_number or f"Xero invoice {self.xero_invoice_id[:8]}"


def document_upload_path(instance: "Document", filename: str) -> str:
    return f"deals/{instance.deal_id}/documents/{filename}"


class Document(TimestampedModel):
    """A document the deal needs. Staff create the request (name + required);
    the customer (or staff) uploads a file, flipping status to 'provided'."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PROVIDED = "provided", "Provided"

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=255, help_text="e.g. Bank statements, Photo ID, Accounts")
    required = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUESTED)

    file = models.FileField(upload_to=document_upload_path, null=True, blank=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )

    class Meta:
        ordering = ["-required", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"

    @property
    def is_provided(self) -> bool:
        return self.status == self.Status.PROVIDED

    def attach(self, uploaded_file, *, by=None) -> None:
        """Attach an uploaded file and mark the document provided."""
        self.file = uploaded_file
        self.status = self.Status.PROVIDED
        self.uploaded_at = timezone.now()
        self.uploaded_by = by
        self.save()


class Note(TimestampedModel):
    """A note attached to a contact, organisation and/or deal.

    Initially used to port notes and logged emails from HubSpot; `datetime`
    holds when the note was originally written (not when it was imported)."""

    class Type(models.TextChoices):
        HUBSPOT_NOTE = "hubspot_note", "HubSpot note"
        HUBSPOT_EMAIL = "hubspot_email", "HubSpot email"
        HUBSPOT_MIGRATION_COMMENT = "hubspot_migration_comment", "HubSpot migration comment"
        ADMIN_COMMENT = "admin_comment", "Admin comment"

    type = models.CharField(max_length=32, choices=Type.choices)

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True, related_name="notes")
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, null=True, blank=True, related_name="notes")
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, null=True, blank=True, related_name="notes")

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notes",
    )
    content = models.TextField()
    datetime = models.DateTimeField(db_index=True, help_text="When the note was originally written or the email sent.")

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    class Meta:
        ordering = ["-datetime"]
        indexes = [models.Index(fields=["deal", "-datetime"], name="crm_note_deal_datetime")]

    def __str__(self) -> str:
        return f"{self.get_type_display()} — {self.datetime:%Y-%m-%d %H:%M}"
