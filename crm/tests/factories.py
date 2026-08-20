"""Lightweight object builders for tests — kept dependency-free so we don't
have to ship factory_boy. Each helper accepts overrides via keyword args."""

from __future__ import annotations

import itertools
from decimal import Decimal

from django.contrib.auth import get_user_model

from django.utils import timezone

from accounts.models import ApiKey, Role
from crm.models import (
    Contact,
    Deal,
    Document,
    Organisation,
    Participation,
    Proposal,
    Quote,
    RateBand,
    SignatureRequest,
    XeroConnection,
    XeroInvoice,
)

User = get_user_model()

# Unique counter so tests that build several records in one TestCase don't
# collide on unique fields (User.email, Organisation.name doesn't have to be
# unique but reads better as distinct).
_seq = itertools.count(1)


def _next() -> int:
    return next(_seq)


def make_user(*, role: str = Role.ASSOCIATE, email: str | None = None,
              password: str = "testpass123!", **extra) -> "User":
    n = _next()
    email = email or f"{role}-{n}@example.com"
    return User.objects.create_user(email=email, password=password, role=role, **extra)


def make_admin(**extra) -> "User":
    return make_user(role=Role.ADMIN, **extra)


def make_associate(**extra) -> "User":
    return make_user(role=Role.ASSOCIATE, **extra)


def make_customer(**extra) -> "User":
    return make_user(role=Role.CUSTOMER, **extra)


def make_organisation(**extra) -> Organisation:
    extra.setdefault("name", f"Org {_next()}")
    return Organisation.objects.create(**extra)


def make_contact(*, user=None, organisation: Organisation | None = None,
                 **extra) -> Contact:
    n = _next()
    extra.setdefault("first_name", "Test")
    extra.setdefault("last_name", f"Contact-{n}")
    extra.setdefault("email", f"contact-{n}@example.com")
    contact = Contact.objects.create(user=user, **extra)
    if organisation is not None:
        contact.organisations.add(organisation)
    return contact


def make_deal(*, owner=None, customer: Contact | None = None,
              organisation: Organisation | None = None, **extra) -> Deal:
    owner = owner or make_associate()
    organisation = organisation or make_organisation()
    customer = customer or make_contact(organisation=organisation)
    extra.setdefault("name", f"Deal {_next()}")
    return Deal.objects.create(
        owner=owner, customer=customer, organisation=organisation, **extra,
    )


def make_customer_with_deal(*, owner=None) -> tuple["User", Deal]:
    """The most common portal-test fixture: a customer user whose contact owns
    one deal. Returns ``(user, deal)``."""
    user = make_customer()
    org = make_organisation()
    contact = make_contact(user=user, organisation=org)
    deal = make_deal(owner=owner, customer=contact, organisation=org)
    return user, deal


def make_rate_band(*, organisation: Organisation | None = None,
                   term_months: int = 60, min_amount: int = 1_000,
                   max_amount: int = 1_000_000,
                   yield_percent: Decimal | str = "10.00") -> RateBand:
    return RateBand.objects.create(
        organisation=organisation or make_organisation(name=f"Lender {_next()}"),
        term_months=term_months,
        min_amount=min_amount,
        max_amount=max_amount,
        yield_percent=Decimal(yield_percent),
    )


def make_quote(deal: Deal, *, rate: RateBand | None = None,
               term: int = 60) -> Quote:
    rate = rate or make_rate_band(term_months=term)
    return Quote.objects.create(deal=deal, rate=rate, term=term)


def make_proposal(deal: Deal, *, lender: Organisation | None = None,
                  **extra) -> Proposal:
    lender = lender or make_organisation(name=f"Lender {_next()}")
    return Proposal.objects.create(deal=deal, lender=lender, **extra)


def make_participation(deal: Deal, *, organisation: Organisation | None = None,
                       amount: Decimal | str = "10000.00") -> Participation:
    return Participation.objects.create(
        deal=deal,
        organisation=organisation,
        amount=Decimal(amount),
    )


def make_document(deal: Deal, *, name: str = "Bank statements") -> Document:
    return Document.objects.create(deal=deal, name=name)


def make_signature_request(deal: Deal, *, submission_id: int | None = None,
                           **extra) -> SignatureRequest:
    extra.setdefault("template_id", 1)
    extra.setdefault("template_name", "Test agreement")
    extra.setdefault("signer_email", f"signer-{_next()}@example.com")
    return SignatureRequest.objects.create(
        deal=deal,
        submission_id=submission_id or _next() + 10_000,
        **extra,
    )


def make_xero_connection(**extra) -> XeroConnection:
    """A live-looking connection: token expiry in the future so
    `xero.get_active_connection` doesn't try to refresh over the network."""
    extra.setdefault("tenant_id", f"tenant-{_next()}")
    extra.setdefault("tenant_name", "Demo Company (UK)")
    extra.setdefault("access_token", "test-access-token")
    extra.setdefault("refresh_token", "test-refresh-token")
    extra.setdefault("expires_at", timezone.now() + timezone.timedelta(minutes=25))
    return XeroConnection.objects.create(**extra)


def make_xero_invoice(deal: Deal, **extra) -> XeroInvoice:
    n = _next()
    extra.setdefault("xero_invoice_id", f"00000000-0000-0000-0000-{n:012d}")
    extra.setdefault("xero_invoice_number", f"INV-{n:04d}")
    extra.setdefault("contact_name", "Test Contact")
    extra.setdefault("status", "AUTHORISED")
    return XeroInvoice.objects.create(deal=deal, **extra)


def make_api_key(*, organisation: Organisation | None = None,
                 created_by=None,
                 is_active: bool = True) -> tuple[ApiKey, str]:
    """Mint an ApiKey. Returns ``(instance, raw_key)``; tests usually want
    both — the instance to assert on, the raw key to pass as a Bearer token.
    Auto-creates the owning Organisation when one isn't supplied."""
    organisation = organisation or make_organisation(name=f"Integrator {_next()}")
    instance, raw = ApiKey.issue(organisation=organisation, created_by=created_by)
    if not is_active:
        instance.is_active = False
        instance.save(update_fields=["is_active"])
    return instance, raw
