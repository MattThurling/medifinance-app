"""Transactional email helpers.

These build multipart (text + HTML) messages and send via whatever
EMAIL_BACKEND is configured: the console locally, the Mailtrap sandbox over
SMTP on Cloud Run dev, and the Mailgun HTTP API (django-anymail) on prod.
"""

from __future__ import annotations

import time
from email.mime.image import MIMEImage
from functools import lru_cache
from smtplib import SMTPDataError

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

# Logo embedded in every HTML email. templates/email/_base.html references it
# as <img src="cid:medifinance-logo">. Inline (CID) attachment rather than a
# hosted URL so the logo shows without the client fetching from our server
# (hosted images are blocked by default in Outlook and many corporate
# clients), and PNG rather than the site's WebP, which Outlook can't render.
LOGO_PATH = settings.BASE_DIR / "static" / "img" / "MedifinanceLogo.png"
LOGO_CID = "medifinance-logo"


def _send_with_retry(message: EmailMultiAlternatives) -> None:
    """Send, retrying when the mailserver throttles.

    The Mailtrap sandbox (dev email) rate-limits sends and rejects the excess
    with `550 Too many emails per second` — which happens whenever a flow
    sends two emails back-to-back (e.g. API deal create sends the staff
    notification then the customer magic link). Observed behaviour is that
    rejected attempts keep the penalty window open, so retries back off
    exponentially (2s, 4s, 8s) rather than at a fixed short interval. Any
    other failure propagates unchanged.
    """
    attempts = 4
    for attempt in range(attempts):
        try:
            message.send()
            return
        except SMTPDataError as exc:
            throttled = exc.smtp_code == 550 and b"too many emails" in exc.smtp_error.lower()
            if not throttled or attempt == attempts - 1:
                raise
            time.sleep(2 * 2 ** attempt)


@lru_cache(maxsize=1)
def _logo_bytes() -> bytes:
    return LOGO_PATH.read_bytes()


def _build_message(*, subject: str, text_body: str, html_body: str, to: list[str]) -> EmailMultiAlternatives:
    """Assemble a branded multipart message: text + HTML alternatives plus the
    inline logo, as multipart/related so clients render the logo in place
    rather than listing it as an attachment."""
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        reply_to=[settings.EMAIL_REPLY_TO],
        to=to,
    )
    message.attach_alternative(html_body, "text/html")
    message.mixed_subtype = "related"
    logo = MIMEImage(_logo_bytes(), _subtype="png")
    logo.add_header("Content-ID", f"<{LOGO_CID}>")
    logo.add_header("Content-Disposition", "inline", filename="MedifinanceLogo.png")
    message.attach(logo)
    return message


def send_magic_link_email(*, to_email: str, link_url: str, deal_name: str, owner_name: str,
                          expires_at, via_partner: str | None = None) -> None:
    """Email a customer their single-use portal link.

    `via_partner` names the partner (e.g. a supplier's website or AI agent)
    that started the application through the partner API. When set, the email
    explains who asked for it and makes clear the recipient can ignore it if
    they weren't expecting it — a guest or an agent may have typed the wrong
    address, and nothing proceeds unless the recipient opens the link."""
    context = {
        "link_url": link_url,
        "deal_name": deal_name,
        "owner_name": owner_name,
        "expires_at": expires_at,
        "via_partner": via_partner,
    }
    subject = "Your Medifinance application link"
    text_body = render_to_string("email/magic_link.txt", context)
    html_body = render_to_string("email/magic_link.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=[to_email]))


def send_new_deal_notification_email(
    *,
    deal_name: str,
    deal_url: str,
    introducer_name: str,
    customer_name: str,
    customer_email: str,
    amount_display: str,
    is_limited_company: bool,
    quote_count: int,
) -> None:
    """Tell the NOTIFY_EMAILS staff list that an integrator created a deal."""
    context = {
        "deal_name": deal_name,
        "deal_url": deal_url,
        "introducer_name": introducer_name,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "amount_display": amount_display,
        "is_limited_company": is_limited_company,
        "quote_count": quote_count,
    }
    subject = f"New API deal: {deal_name} ({amount_display})"
    text_body = render_to_string("email/new_deal_notification.txt", context)
    html_body = render_to_string("email/new_deal_notification.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=settings.NOTIFY_EMAILS))


def send_supplier_invoice_submitted_email(
    *,
    deal_name: str,
    deal_url: str,
    supplier_name: str,
    client_org_name: str,
    amount_display: str,
    invoice_number: str,
) -> None:
    """Tell the NOTIFY_EMAILS staff list that a supplier uploaded their invoice."""
    context = {
        "deal_name": deal_name,
        "deal_url": deal_url,
        "supplier_name": supplier_name,
        "client_org_name": client_org_name,
        "amount_display": amount_display,
        "invoice_number": invoice_number,
    }
    subject = f"Supplier invoice submitted: {client_org_name} ({deal_name})"
    text_body = render_to_string("email/supplier_invoice_submitted.txt", context)
    html_body = render_to_string("email/supplier_invoice_submitted.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=settings.NOTIFY_EMAILS))


def send_applicant_added_email(
    *,
    deal_name: str,
    deal_url: str,
    added_by_name: str,
    applicant_name: str,
    applicant_email: str,
) -> None:
    """Tell the NOTIFY_EMAILS staff list that a customer added a co-applicant
    from the portal — the person is unverified until staff have checked them."""
    context = {
        "deal_name": deal_name,
        "deal_url": deal_url,
        "added_by_name": added_by_name,
        "applicant_name": applicant_name,
        "applicant_email": applicant_email,
    }
    subject = f"Applicant added by customer: {applicant_name} ({deal_name})"
    text_body = render_to_string("email/applicant_added.txt", context)
    html_body = render_to_string("email/applicant_added.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=settings.NOTIFY_EMAILS))


def send_commission_invoice_request_email(
    *,
    deal_name: str,
    deal_url: str,
    customer_name: str,
    client_org_name: str,
    lender_org_name: str,
    proposal_number: str,
    finance_amount_display: str,
    commission_display: str,
    requested_by: str,
) -> None:
    """Tell the ACCOUNTS_EMAILS list to raise a commission invoice for a deal."""
    context = {
        "deal_name": deal_name,
        "deal_url": deal_url,
        "customer_name": customer_name,
        "client_org_name": client_org_name,
        "lender_org_name": lender_org_name,
        "proposal_number": proposal_number,
        "finance_amount_display": finance_amount_display,
        "commission_display": commission_display,
        "requested_by": requested_by,
    }
    subject = f"Commission invoice request: {deal_name}"
    text_body = render_to_string("email/commission_invoice_request.txt", context)
    html_body = render_to_string("email/commission_invoice_request.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=settings.ACCOUNTS_EMAILS))


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

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=[to_email]))


def send_proposal_approved_client_email(
    *,
    to_email: str,
    contact_first_name: str,
    lender_org_name: str,
    proposal_number: str,
    finance_amount_display: str,
    term_display: str,
    monthly_payment_display: str,
) -> None:
    """Tell the client the good news that their finance has been approved.

    Optional details (proposal number, finance amount, term, monthly payment)
    are passed as "" when unknown and their lines are omitted from the body —
    this is client-facing, so no "—" placeholders.
    """
    context = {
        "contact_first_name": contact_first_name,
        "lender_org_name": lender_org_name,
        "proposal_number": proposal_number,
        "finance_amount_display": finance_amount_display,
        "term_display": term_display,
        "monthly_payment_display": monthly_payment_display,
    }
    subject = f"Good news — your finance has been approved by {lender_org_name}"
    text_body = render_to_string("email/proposal_approved_client.txt", context)
    html_body = render_to_string("email/proposal_approved_client.html", context)

    _send_with_retry(_build_message(subject=subject, text_body=text_body, html_body=html_body, to=[to_email]))
