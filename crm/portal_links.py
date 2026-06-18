"""Mint customer portal magic links for a Deal.

Pulled out of `_PortalLinkMixin` so the API path (which has no `request.user`
and no `messages` framework available) can reuse the exact same logic.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.models import MagicLink, Role


class NoCustomerEmailError(Exception):
    """The Deal's customer Contact has no email on file."""


def issue_portal_link_for_deal(deal, *, created_by=None):
    """Resolve (or create) the customer User for `deal.customer` and issue a
    single-use MagicLink that lands on the deal's quote-select page.

    Returns ``(link, duplicate_user_warning)`` where the second element is a
    human-readable string when the contact's email is already attached to a
    different Contact (the link still works), or ``None`` otherwise.

    Raises ``NoCustomerEmailError`` if the contact has no email — magic links
    are useless without one. Callers should surface this to the user.
    """
    from .models import Contact

    contact = deal.customer

    if contact.user is not None:
        link_user = contact.user
        dup_warning = None
    else:
        if not contact.email:
            raise NoCustomerEmailError(
                "Can't issue a link: this contact has no email address on file."
            )
        User = get_user_model()
        # Reuse an existing user with that email if one already exists
        # (e.g. imported from HubSpot, or shared across contacts).
        link_user, was_created = User.objects.get_or_create(
            email=contact.email,
            defaults={
                "role": Role.CUSTOMER,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
            },
        )
        if was_created:
            link_user.set_unusable_password()
            link_user.save(update_fields=["password"])

        # Contact.user is OneToOne: don't try to relink if this user is
        # already attached to another contact. The link still works.
        other = Contact.objects.filter(user=link_user).exclude(pk=contact.pk).first()
        if other is not None:
            dup_warning = (
                f"Heads up: the email {contact.email} is already linked to contact "
                f"“{other}”. The link works fine, but this contact won't be "
                f"re-linked to the user. Consider deduplicating the contacts."
            )
        else:
            contact.user = link_user
            contact.save(update_fields=["user"])
            dup_warning = None

    link = MagicLink.issue(
        user=link_user,
        redirect_url=reverse("crm:portal_quote_select", args=[deal.pk]),
        created_by=created_by,
    )
    return link, dup_warning
