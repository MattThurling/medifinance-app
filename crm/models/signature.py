from django.conf import settings
from django.db import models

from .base import TimestampedModel
from .contact import Contact
from .deal import Deal


def signature_audit_upload_path(instance: "SignatureRequest", filename: str) -> str:
    return f"deals/{instance.deal_id}/signatures/audit/{filename}"


def signature_signed_upload_path(instance: "SignatureRequest", filename: str) -> str:
    return f"deals/{instance.deal_id}/signatures/{filename}"


class SignatureRequest(TimestampedModel):
    """A DocuSeal submission sent for e-signature on a Deal.

    The signed PDF is stored on `signed_file` when the signer completes; this
    row keeps the DocuSeal linkage plus the audit trail of who signed, when
    and from where."""

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        OPENED = "opened", "Opened"
        COMPLETED = "completed", "Completed"
        DECLINED = "declined", "Declined"
        VOIDED = "voided", "Voided"

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="signature_requests")
    template_id = models.PositiveIntegerField()
    template_name = models.CharField(max_length=255, blank=True)
    submission_id = models.PositiveIntegerField(unique=True)
    submitter_id = models.PositiveIntegerField(null=True, blank=True)
    # DocuSeal's per-signer slug: the signing page lives at {DOCUSEAL_URL}/s/{slug}.
    # Captured at creation; lazily backfilled for older rows (MyDealSignView).
    signing_slug = models.CharField(max_length=255, blank=True, default="")

    signer = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="signature_requests",
    )
    # Snapshots — the Contact's details may change after sending.
    signer_email = models.EmailField()
    signer_name = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SENT)
    opened_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True)

    signer_ip = models.GenericIPAddressField(null=True, blank=True)
    signer_user_agent = models.CharField(max_length=512, blank=True)
    signed_file = models.FileField(upload_to=signature_signed_upload_path, null=True, blank=True)
    audit_log_file = models.FileField(upload_to=signature_audit_upload_path, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="signature_requests_sent",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.template_name or f"Submission {self.submission_id}"
        return f"{label} → {self.signer_email} ({self.get_status_display()})"

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.SENT, self.Status.OPENED)

