"""Transactional email helpers.

These build multipart (text + HTML) messages and send via whatever
EMAIL_BACKEND is configured (console in dev, Mailgun in prod).
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_magic_link_email(*, to_email: str, link_url: str, deal_name: str, owner_name: str, expires_at) -> None:
    """Email a customer their single-use portal link."""
    context = {
        "link_url": link_url,
        "deal_name": deal_name,
        "owner_name": owner_name,
        "expires_at": expires_at,
    }
    subject = "Your Medifinance application link"
    text_body = render_to_string("email/magic_link.txt", context)
    html_body = render_to_string("email/magic_link.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    message.attach_alternative(html_body, "text/html")
    message.send()


def send_supplier_invoice_request_email(
    *,
    to_email: str,
    link_url: str,
    contact_first_name: str,
    lead_contact_name: str,
    client_org_name: str,
    client_org_address: str,
    lender_org_name: str,
    lender_org_address: str,
    participation_amount_display: str,
    participation_description: str,
    expires_at,
) -> None:
    """Ask a supplier to upload their invoice for a deal Participation.

    Subject: 'Request for Supplier Invoice for <client org name>'.
    Body contains the deal lead + client + lender details, the participation
    amount (and optional description), and a single-use upload link.
    """
    context = {
        "link_url": link_url,
        "contact_first_name": contact_first_name,
        "lead_contact_name": lead_contact_name,
        "client_org_name": client_org_name,
        "client_org_address": client_org_address,
        "lender_org_name": lender_org_name,
        "lender_org_address": lender_org_address,
        "participation_amount_display": participation_amount_display,
        "participation_description": participation_description,
        "expires_at": expires_at,
    }
    subject = f"Request for Supplier Invoice for {client_org_name}"
    text_body = render_to_string("email/supplier_invoice_request.txt", context)
    html_body = render_to_string("email/supplier_invoice_request.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    message.attach_alternative(html_body, "text/html")
    message.send()
