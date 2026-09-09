import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from .base import TimestampedModel
from .contact import Contact
from .deal import Deal
from .organisation import Organisation


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

