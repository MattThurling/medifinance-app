from django.conf import settings
from django.db import models
from django.utils import timezone

from .base import TimestampedModel
from .deal import Deal
from .organisation import Organisation


class Stage(TimestampedModel):
    """A stage-change event on a Deal. The latest one is the deal's current stage."""

    class Name(models.TextChoices):
        INITIAL_CONTACT = "initial_contact", "Initial Contact"
        APPLICATION = "application", "Application"
        INFO_RECEIVED = "info_received", "Info Received"
        QUOTING = "quoting", "Quoting"
        PROPOSAL_SUBMITTED = "proposal_submitted", "Proposal Submitted"
        PROPOSAL_APPROVED = "proposal_approved", "Proposal Approved"
        PROPOSAL_DECLINED = "proposal_declined", "Proposal Declined"
        CLIENT_NOTIFIED = "client_notified", "Client Notified"
        DOCUMENTS_OUT = "documents_out", "Documents Out"
        LEGALS = "legals", "Legals"
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

