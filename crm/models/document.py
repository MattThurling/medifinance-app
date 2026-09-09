from django.conf import settings
from django.db import models
from django.utils import timezone

from .base import TimestampedModel
from .deal import Deal


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

