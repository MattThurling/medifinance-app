from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from .base import TimestampedModel, _hubspot_url
from .contact import Contact
from .organisation import Organisation


class Deal(TimestampedModel):
    class Type(models.TextChoices):
        ASSET_FINANCE = "asset_finance", "Asset Finance"
        COMMERCIAL_FINANCE = "commercial_finance", "Commercial Finance"
        BUSINESS_LOANS = "business_loans", "Business Loans"
        STRUCTURED_FINANCE = "structured_finance", "Structured Finance"
        KMC_ASSOCIATES = "kmc_associates", "KMC Associates"
        SHORT_TERM_FINANCE = "short_term_finance", "Short Term Finance"
        PRACTICE_PURCHASE = "practice_purchase", "Practice Purchase"
        REFINANCE = "refinance", "Refinance"
        RENOVATION = "renovation", "Renovation"
        STARTUP = "startup", "Startup"
        PROPERTY_PURCHASE = "property_purchase", "Property Purchase"
        BRIDGING_LOAN = "bridging_loan", "Bridging Loan"
        TAX_LOAN = "tax_loan", "Tax Loan"
        PARTNER_BUY_IN = "partner_buy_in", "Partner Buy-in"

    # Types no longer offered on the deal form. They stay in ``Type`` so
    # existing deals keep their label and still validate on edit; the form
    # only shows one of these when the deal being edited already has it.
    RETIRED_TYPES = frozenset({
        Type.COMMERCIAL_FINANCE,
        Type.STRUCTURED_FINANCE,
        Type.KMC_ASSOCIATES,
        Type.SHORT_TERM_FINANCE,
    })

    class Source(models.TextChoices):
        INTRODUCER = "introducer", "Introducer"
        EXISTING_CUSTOMER = "existing_customer", "Existing Customer"
        SUPPLIER = "supplier", "Supplier"
        REFERRAL = "referral", "Referral"
        EXHIBITION = "exhibition", "Exhibition"
        MEETING = "meeting", "Meeting"
        WEBSITE = "website", "Website"
        SEARCH_ENGINE = "search_engine", "Search Engine"
        CAMPAIGN_ADVERT = "campaign_advert", "Campaign / Advert"
        E_SHOT = "e_shot", "E-shot"
        TWITTER = "twitter", "Twitter"
        OTHER = "other", "Other"

    name = models.CharField(max_length=255)
    type = models.CharField(
        max_length=32,
        choices=Type.choices,
        null=True,
        blank=True,
        help_text="The kind of finance this deal is for. Optional.",
    )
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        null=True,
        blank=True,
        help_text="How this deal came to MediFinance. Optional.",
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

    class Channel(models.TextChoices):
        """How a partner-API application was submitted. `web` is the partner's
        own site with a signed-in customer; `web-guest` the same site with no
        account; `mcp` an AI agent (Claude, ChatGPT…) via the partner's MCP
        server; `api` a direct server-to-server integration."""
        WEB = "web", "Web (signed in)"
        WEB_GUEST = "web-guest", "Web (guest)"
        MCP = "mcp", "AI agent (MCP)"
        API = "api", "API"

    class CompanyType(models.TextChoices):
        LIMITED_COMPANY = "limited_company", "Limited company"
        SOLE_TRADER = "sole_trader", "Sole trader"
        PARTNERSHIP = "partnership", "Partnership"
        OTHER = "other", "Other"

    # Partner-API attribution. All optional/defaulted: only set on deals that
    # arrived via POST /api/deals/, and only when the partner supplied them.
    channel = models.CharField(
        max_length=32, choices=Channel.choices, blank=True, default="",
        help_text="Submission channel reported by the partner (web, web-guest, mcp, api).",
    )
    agent_client = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Free text: the AI client that submitted (e.g. claude, chatgpt). Informational only.",
    )
    partner_product_id = models.CharField(
        max_length=100, blank=True, default="",
        help_text="The partner's own product/SKU id for the asset being financed.",
    )
    asset_category = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Partner-supplied asset category (e.g. imaging, surgery).",
    )
    company_type = models.CharField(
        max_length=32, choices=CompanyType.choices, blank=True, default="",
        help_text="The applicant's business type as given at application.",
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
    initial_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    final_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    commission = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    document_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    non_completion_fee = models.DecimalField(
        "Non-completion fee %",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Charged as a percentage if the deal doesn't complete.",
    )

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
                  "payment date + the selected quote's committed term (or "
                  "full term).",
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
        """Whether this deal uses the Commercial Finance form treatment
        (funded amounts instead of supplier participations).

        Every deal type except Asset Finance does. Untyped deals keep the
        Asset Finance-style (full supplier) treatment."""
        return self.type is not None and self.type != Deal.Type.ASSET_FINANCE

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
        """When this deal matures for re-engagement. The explicit
        `term_end_date` (set by staff or the HubSpot migration) wins;
        otherwise derived as the end of the selected quote's committed period
        — the rate review, when there is one — else its full term. None when
        neither is available."""
        if self.term_end_date:
            return self.term_end_date
        quote = self.selected_quote
        if self.first_payment_date is None or quote is None:
            return None
        from .. import pricing  # avoid circular import; pricing imports RateBand

        return pricing.add_months(self.first_payment_date, quote.priced_term - 1)

    @property
    def repayment_schedule(self) -> "list[dict] | None":
        """Amortisation schedule for the selected quote, starting at
        `first_payment_date`. None until both are set (and the quote's monthly
        payment is computable) — see `crm.pricing.repayment_schedule`."""
        from .. import pricing  # avoid circular import; pricing imports RateBand

        quote = self.selected_quote
        if self.first_payment_date is None or quote is None:
            return None
        rows = pricing.repayment_schedule(
            principal=quote.finance_amount,
            monthly_payment=quote.monthly_payment,
            term_months=quote.term,
            first_payment_date=self.first_payment_date,
        )
        # Beyond the committed window the rate is subject to review, so those
        # payments are indicative. Presentation concern only — the shared
        # pricing maths stay untouched.
        if rows and quote.commitment_months and quote.commitment_months < len(rows):
            for row in rows[quote.commitment_months:]:
                row["indicative"] = True
        return rows

    @property
    def has_committed_quote(self) -> bool:
        """Whether any quote on this deal has a rate commitment — drives the
        'Committed for' column. Iterates `quotes.all()` so a prefetch stays
        cheap."""
        return any(q.commitment_months for q in self.quotes.all())

