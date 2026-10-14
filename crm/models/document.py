import zipfile
from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

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

    def attach(self, uploaded_files, *, by=None) -> None:
        """Attach an upload and mark the document provided. Accepts one file
        or a list: a single file is stored as-is (so a PDF still opens inline);
        several are bundled into one zip named after the request."""
        files = list(uploaded_files) if isinstance(uploaded_files, (list, tuple)) else [uploaded_files]
        self.file = files[0] if len(files) == 1 else self._zip(files)
        self.status = self.Status.PROVIDED
        self.uploaded_at = timezone.now()
        self.uploaded_by = by
        self.save()

    def _zip(self, files) -> ContentFile:
        """Bundle uploaded files into an in-memory zip, de-duplicating entry
        names ("statement.pdf", "statement (2).pdf", …)."""
        buffer = BytesIO()
        seen: set[str] = set()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for f in files:
                name = f.name.rsplit("/", 1)[-1]
                stem, dot, ext = name.rpartition(".")
                if not dot:
                    stem, ext = name, ""
                candidate, n = name, 1
                while candidate in seen:
                    n += 1
                    candidate = f"{stem} ({n}).{ext}" if dot else f"{stem} ({n})"
                seen.add(candidate)
                f.seek(0)
                archive.writestr(candidate, f.read())
        return ContentFile(buffer.getvalue(), name=f"{slugify(self.name) or 'documents'}.zip")
