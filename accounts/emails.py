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
