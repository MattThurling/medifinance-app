from django.conf import settings
from django.db import models
from django.utils import timezone

from .deal import Deal
from .organisation import Organisation


class PartnerApiRequest(models.Model):
    """One row per `POST /api/deals/` attempt — the partner API's audit log and
    its idempotency store.

    Every submission is recorded whatever the outcome (created, dedup'd,
    rejected, rate-limited, sandbox…) so compliance can later answer "who sent
    what, through which channel, and what did we do with it". Successful
    responses are stored verbatim so a retry carrying the same
    ``Idempotency-Key`` can be answered with the original result instead of
    creating a second deal or sending a second email.
    """

    class Outcome(models.TextChoices):
        CREATED = "created", "Created"
        DEDUPLICATED = "deduplicated", "Deduplicated"
        REPLAYED = "replayed", "Replayed (idempotent retry)"
        SANDBOX = "sandbox", "Sandbox"
        REJECTED = "rejected", "Rejected"
        RATE_LIMITED = "rate_limited", "Rate limited"
        CONFLICT = "conflict", "Idempotency key conflict"
        DISABLED = "disabled", "API disabled"

    api_key = models.ForeignKey(
        "accounts.ApiKey",
        on_delete=models.PROTECT,
        related_name="requests",
    )
    organisation = models.ForeignKey(
        Organisation,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="api_requests",
        help_text="The partner that sent the request (the key's organisation).",
    )
    idempotency_key = models.CharField(max_length=255, blank=True, db_index=True)
    request_hash = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of the canonicalised request body, so a reused "
                  "idempotency key with a different body can be detected.",
    )
    channel = models.CharField(max_length=32, blank=True)
    agent_client = models.CharField(max_length=64, blank=True)
    email = models.EmailField(
        blank=True,
        help_text="The applicant email on the request, if it parsed.",
    )
    outcome = models.CharField(max_length=32, choices=Outcome.choices)
    status_code = models.PositiveSmallIntegerField()
    response_body = models.JSONField(null=True, blank=True)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="api_requests",
    )
    replay_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="replays",
        help_text="For an idempotent retry, the original request it replayed.",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["api_key", "idempotency_key"],
                         name="crm_apireq_key_idem"),
        ]

    def __str__(self) -> str:
        return f"{self.get_outcome_display()} {self.status_code} · {self.created_at:%d %b %Y %H:%M}"
