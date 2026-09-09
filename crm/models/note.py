from django.conf import settings
from django.db import models

from .base import TimestampedModel
from .contact import Contact
from .deal import Deal
from .organisation import Organisation


class Note(TimestampedModel):
    """A note attached to a contact, organisation and/or deal.

    Initially used to port notes and logged emails from HubSpot; `datetime`
    holds when the note was originally written (not when it was imported)."""

    class Type(models.TextChoices):
        HUBSPOT_NOTE = "hubspot_note", "HubSpot note"
        HUBSPOT_EMAIL = "hubspot_email", "HubSpot email"
        HUBSPOT_MIGRATION_COMMENT = "hubspot_migration_comment", "HubSpot migration comment"
        ADMIN_COMMENT = "admin_comment", "Admin comment"
        CUSTOMER_UPDATE = "customer_update", "Customer update"

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
