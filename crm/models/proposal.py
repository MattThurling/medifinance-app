from django.db import models

from .base import TimestampedModel
from .contact import Contact
from .deal import Deal
from .organisation import Organisation


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
    notified_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the client was emailed the approval news for this proposal.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.lender.name} ({self.get_status_display()})"

