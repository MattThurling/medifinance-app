from django.conf import settings
from django.db import models


def _hubspot_url(object_type: str, hubspot_id: str | None) -> str | None:
    """Build a deep-link to a record in HubSpot, or None if there's no hubspot_id.

    `object_type` is HubSpot's CRM object code: 0-1 contact, 0-2 company, 0-3 deal.
    """
    if not hubspot_id:
        return None
    portal = settings.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{portal}/record/{object_type}/{hubspot_id}"


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

