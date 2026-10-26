"""Attach and detach co-applicants on a deal.

Shared by the staff deal page and the customer portal so both record the
same through-row details and apply the same removal rules. Kept out of
``crm.views`` (like ``crm.portal_links``) so it has no request dependency.
"""

from __future__ import annotations

from django.utils import timezone

from .models import Contact, Document, Note, SignatureRequest


class NoOrganisationError(Exception):
    """The deal has no organisation, so there is no company to add people from."""


def add_applicant(deal, *, first_name: str, last_name: str, email: str, phone: str = "",
                  by_user, by_customer: bool) -> Contact:
    """Create a brand-new Contact in the deal's organisation and attach it as
    a co-applicant.

    Always creates a fresh Contact. Emails are not unique across contacts, and
    matching an existing record by email would let a portal customer attach
    (and then view and edit) someone else's contact — staff can merge
    duplicates later from the CRM.

    When a customer did it, a ``customer_update`` Note goes on the deal so
    staff see it in the timeline. Raises ``NoOrganisationError`` when the
    deal has no organisation (partner-API deals start without one).
    """
    if deal.organisation_id is None:
        raise NoOrganisationError("Set the deal's organisation before adding applicants.")
    contact = Contact.objects.create(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        owner=deal.owner,
    )
    contact.organisations.add(deal.organisation_id)
    deal.co_applicants.add(
        contact,
        through_defaults={
            "added_by_customer": by_customer,
            "added_by": by_user,
            "added_at": timezone.now(),
        },
    )
    if by_customer:
        Note.objects.create(
            type=Note.Type.CUSTOMER_UPDATE,
            author=by_user,
            datetime=timezone.now(),
            content=f"{by_user.full_name} added {contact} as an applicant.",
            deal=deal,
            contact=contact,
        )
    return contact


def removal_blocker(deal, contact) -> str | None:
    """Why ``contact`` can't be removed from ``deal`` right now, or ``None``.

    Someone with a signature request (in any state — a signed agreement is a
    record of who the parties were) or an upload on the deal is part of its
    history and stays attached; staff can void the request first if the
    person really was added by mistake.
    """
    if SignatureRequest.objects.filter(deal=deal, signer=contact).exists():
        return f"{contact} has a signature request on this application, so they can't be removed."
    if contact.user_id and Document.objects.filter(deal=deal, uploaded_by_id=contact.user_id).exists():
        return f"{contact} has uploaded documents on this application, so they can't be removed."
    return None


def remove_applicant(deal, contact, *, by_user, log: bool) -> None:
    """Detach ``contact`` from ``deal``. The Contact itself is kept — it is a
    CRM record that may be on other deals. ``log`` adds a deal Note (used when
    a customer did it)."""
    deal.co_applicants.remove(contact)
    if log:
        Note.objects.create(
            type=Note.Type.CUSTOMER_UPDATE,
            author=by_user,
            datetime=timezone.now(),
            content=f"{by_user.full_name} removed {contact} as an applicant.",
            deal=deal,
            contact=contact,
        )
