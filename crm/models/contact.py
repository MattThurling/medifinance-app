from django.conf import settings
from django.db import models
from django.urls import reverse

from .base import TimestampedModel, _hubspot_url
from .organisation import Organisation


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

    @property
    def one_line_address(self) -> str:
        """Comma-joined non-empty home address parts, for single-line form fields."""
        parts = [
            self.home_address_line1,
            self.home_address_line2,
            self.home_address_city,
            self.home_address_county,
            self.home_address_postcode,
        ]
        return ", ".join(p for p in parts if p)

    def get_absolute_url(self) -> str:
        return reverse("crm:contact_detail", args=[self.pk])

    @property
    def hubspot_url(self) -> str | None:
        return _hubspot_url("0-1", self.hubspot_id)

