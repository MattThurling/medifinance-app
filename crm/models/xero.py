from django.conf import settings
from django.db import models

from .base import TimestampedModel
from .deal import Deal


class XeroConnection(TimestampedModel):
    """The single Xero organisation this CRM is connected to. Holds the OAuth
    tokens — we only need one row (staff connects once per environment)."""

    tenant_id = models.CharField(max_length=64, unique=True)
    tenant_name = models.CharField(max_length=255)
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField(help_text="When the access token stops working (refreshed automatically).")
    scopes = models.TextField(blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="xero_connections",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Xero · {self.tenant_name}"


class XeroInvoice(TimestampedModel):
    """Local mirror of a Xero invoice we raised against a Deal — so we don't
    re-issue and so staff can jump straight back into Xero."""

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="xero_invoices")
    xero_invoice_id = models.CharField(max_length=64, unique=True)
    xero_invoice_number = models.CharField(max_length=100, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    online_invoice_url = models.URLField(blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=32, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="raised_xero_invoices",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.xero_invoice_number or f"Xero invoice {self.xero_invoice_id[:8]}"

