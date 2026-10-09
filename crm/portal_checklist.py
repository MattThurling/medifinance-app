"""What the customer portal still needs from a customer, for one deal.

Derived entirely from existing data — there is no checklist model. An item
is one thing the customer can act on from their deal page: fill in blank key
details for a person or the company, choose a quote, upload a requested
document, sign an outstanding request. Each item carries the anchor of the
card on the deal page where that action lives.

Used by the deal page ("What we still need from you") and the deals list
("N actions needed" badge), so both always agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Contact, Deal, Document, Organisation, SignatureRequest


@dataclass(frozen=True)
class Item:
    label: str
    anchor: str  # fragment id of the card on the deal page, e.g. "quotes"


def contact_missing_fields(contact: Contact) -> list[str]:
    """Blank key personal details, as lower-case labels in display order."""
    missing = []
    if not contact.date_of_birth:
        missing.append("date of birth")
    if not contact.phone:
        missing.append("phone")
    if not (contact.home_address_line1 and contact.home_address_postcode):
        missing.append("home address")
    return missing


def organisation_missing_fields(org: Organisation) -> list[str]:
    """Blank key company details, as lower-case labels in display order."""
    missing = []
    if not (org.address_line1 and org.address_postcode):
        missing.append("address")
    if not org.phone:
        missing.append("phone")
    return missing


def person_anchor(contact: Contact) -> str:
    return f"person-{contact.pk}"


def deal_people(deal: Deal) -> list[Contact]:
    """The deal's customer first, then co-applicants (deduplicated)."""
    people = [deal.customer]
    seen = {deal.customer_id}
    for co in deal.co_applicants.all():
        if co.pk not in seen:
            people.append(co)
            seen.add(co.pk)
    return people


def outstanding_items(deal: Deal, *, user=None) -> list[Item]:
    """Everything still needed on `deal`, in page order.

    `user` (the logged-in customer) only affects wording: their own contact
    is "Your details", anyone else is named."""
    items: list[Item] = []

    for person in deal_people(deal):
        missing = contact_missing_fields(person)
        if missing:
            is_me = user is not None and person.user_id == user.pk
            who = "Your details" if is_me else (person.full_name or person.email or "Co-applicant")
            items.append(Item(f"{who}: {', '.join(missing)}", person_anchor(person)))

    if deal.organisation_id:
        missing = organisation_missing_fields(deal.organisation)
        if missing:
            items.append(Item(f"Company details: {', '.join(missing)}", "company"))

    if deal.selected_quote_id is None and deal.quotes.all():  # .all() honours prefetch
        items.append(Item("Choose your quote", "quotes"))

    for doc in deal.documents.all():
        if doc.status == Document.Status.REQUESTED:
            items.append(Item(f"Upload: {doc.name}", "documents"))

    for sr in deal.signature_requests.all():
        if sr.status in (SignatureRequest.Status.SENT, SignatureRequest.Status.OPENED):
            items.append(Item(f"Sign: {sr.template_name or 'Agreement'}", "signatures"))

    return items
