from django.conf import settings
from django.db import models
from django.urls import reverse

from .base import TimestampedModel, _hubspot_url


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
        help_text="The everyday name staff use to find this org",
    )
    legal_name = models.CharField(
        max_length=255, blank=True,
        help_text="Registered Companies House name",
    )
    trading_name = models.CharField(
        max_length=255, blank=True,
        help_text="When different",
    )
    companies_house_number = models.CharField(
        "Companies House number",
        max_length=10, blank=True, db_index=True,
        help_text="8 digits, or NI/SC/OC + 6 digits (LLPs)",
    )

    # UK structured address. All blank-allowed — fill in as you go.
    address_line1 = models.CharField("Line 1", max_length=255, blank=True, help_text="House number and street")
    address_line2 = models.CharField("Line 2", max_length=255, blank=True, help_text="Flat, suite, etc.")
    address_city = models.CharField("City", max_length=100, blank=True)
    address_county = models.CharField("County", max_length=100, blank=True, help_text="Optional")
    address_postcode = models.CharField("Postcode", max_length=10, blank=True)

    url = models.URLField(max_length=255, blank=True, help_text="Homepage / website URL")
    email = models.EmailField(
        blank=True,
        help_text="Shared / role inbox for the organisation",
    )
    phone = models.CharField(max_length=32, blank=True, help_text="Main number for the organisation")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organisations",
        null=True,
        blank=True,
        help_text="Staff member responsible for this organisation. Optional",
    )

    sector = models.CharField(
        max_length=32,
        choices=Sector.choices,
        null=True,
        blank=True,
        help_text="The sector of operation. Optional",
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
    def formal_name(self) -> str:
        """Name for formal correspondence: the registered legal name, plus
        'trading as <trading name>' when both are set (and differ). Orgs with
        no legal name fall back to the everyday name, then the trading name."""
        if self.legal_name:
            if self.trading_name and self.trading_name != self.legal_name:
                return f"{self.legal_name} trading as {self.trading_name}"
            return self.legal_name
        return self.name or self.trading_name

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

