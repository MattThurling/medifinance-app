"""Emails that tell a deal's owner something happened on their deal.

Four events notify the owner: an e-signature is completed, a quote (or, on a
commercial deal, terms) is selected, a requested document is uploaded, and a
supplier uploads their invoice. The first three go through
``notify_deal_owner``; the supplier-invoice email predates this module and
keeps its own template, but takes its recipients from
``owner_and_staff_recipients``.

Who gets the email (``owner_recipients``):

* the deal's owner, when they are an active user;
* nobody, when the owner did the thing themselves (``actor`` is the owner) —
  they don't need telling;
* the ``NOTIFY_EMAILS`` staff list, when the deal has no (active) owner, so an
  unowned deal — every partner-API deal starts that way — is never silent.

A notification must never break the action that triggered it: failures are
logged and swallowed. Send after the triggering write has committed, not
inside a transaction holding row locks (the mail helper can sleep and retry).
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def owner_recipients(deal, *, actor=None) -> list[str]:
    """Addresses to tell about an event on ``deal``. ``actor`` is the user
    who caused it, if any (None for a supplier or a webhook)."""
    owner = deal.owner
    if owner is None or not owner.is_active or not owner.email:
        return list(settings.NOTIFY_EMAILS)
    if actor is not None and actor.pk == owner.pk:
        return []
    return [owner.email]


def owner_and_staff_recipients(deal) -> list[str]:
    """The deal owner plus the NOTIFY_EMAILS staff list, de-duplicated
    (case-insensitively) with the owner first."""
    recipients: list[str] = []
    seen: set[str] = set()
    for address in (*owner_recipients(deal), *settings.NOTIFY_EMAILS):
        if address.lower() not in seen:
            seen.add(address.lower())
            recipients.append(address)
    return recipients


def notify_deal_owner(request, deal, *, subject: str, headline: str,
                      rows: list[tuple[str, str]] | None = None, actor=None) -> None:
    """Email the deal's owner about an event. ``rows`` are (label, value)
    detail lines; empty values are dropped. Never raises."""
    try:
        recipients = owner_recipients(deal, actor=actor)
        if not recipients:
            return
        from accounts.emails import send_deal_owner_notification_email
        send_deal_owner_notification_email(
            to=recipients,
            subject=subject,
            headline=headline,
            deal_name=deal.name,
            deal_url=request.build_absolute_uri(deal.get_absolute_url()),
            rows=rows or [],
        )
    except Exception:
        logger.exception("Deal-owner notification %r failed for deal %s", subject, deal.pk)


def quote_summary(quote) -> str:
    """'60 months — £123.45/mo', as much of it as the quote can compute."""
    detail = f"{quote.term} months" if quote.term else ""
    monthly = quote.monthly_payment
    if monthly is not None:
        detail += f"{' — ' if detail else ''}£{monthly:,.2f}/mo"
    return detail


def notify_quote_selected(request, deal, quote, *, actor) -> None:
    noun = "Terms" if deal.is_commercial_finance else "Quote"
    lender = quote.lender_org
    notify_deal_owner(
        request, deal, actor=actor,
        subject=f"{noun} selected: {deal.name}",
        headline=(
            f"{noun} {'have' if noun == 'Terms' else 'has'} been selected on a deal you own."
        ),
        rows=[
            ("Selected by", actor.full_name or actor.email),
            (noun, quote_summary(quote)),
            ("Lender", lender.name if lender else ""),
        ],
    )


def notify_document_uploaded(request, document, *, actor, file_count: int = 1) -> None:
    deal = document.deal
    notify_deal_owner(
        request, deal, actor=actor,
        subject=f"Document uploaded: {document.name} ({deal.name})",
        headline="A document has been uploaded to a deal you own.",
        rows=[
            ("Document", document.name),
            ("Uploaded by", actor.full_name or actor.email),
            ("Files", str(file_count) if file_count > 1 else ""),
        ],
    )


def notify_signature_completed(request, signature_request) -> None:
    deal = signature_request.deal
    label = signature_request.template_name or "Agreement"
    notify_deal_owner(
        request, deal,
        subject=f"Signed: {label} ({deal.name})",
        headline="An e-signature request has been signed on a deal you own. "
                 "The signed document is on the deal.",
        rows=[
            ("Document", label),
            ("Signed by", signature_request.signer_name or signature_request.signer_email),
        ],
    )
