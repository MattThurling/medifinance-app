"""Row-level visibility rules.

One place answers "which deals may this user see?" so every list, detail,
child-object lookup and API endpoint agrees. Contacts and organisations are
a shared address book (every staff member sees all of them); only deals, and
the records that hang off a deal, are scoped.

Kept out of ``crm.views`` so ``crm.api``, ``crm.stats`` and ``accounts.views``
can import it without pulling in the whole views package.
"""

from django.shortcuts import get_object_or_404

from .models import Deal


def visible_deals(user):
    """Deals ``user`` may see and act on.

    * admin      -> every deal, including unowned ones (owner is NULL)
    * associate  -> deals they own
    * customer   -> deals where they are the customer contact
    * anonymous / anything else -> none
    """
    if not user.is_authenticated:
        return Deal.objects.none()
    if user.is_admin:
        return Deal.objects.all()
    if user.is_associate:
        return Deal.objects.filter(owner=user)
    if user.is_customer:
        return Deal.objects.filter(customer__user=user)
    return Deal.objects.none()


def scope_to_visible_deals(qs, user, deal_field="deal"):
    """Restrict a queryset of deal children (Quote, Proposal, Participation,
    Document, SignatureRequest, ...) to rows whose deal the user may see.
    Admins skip the subquery entirely."""
    if user.is_authenticated and user.is_admin:
        return qs
    return qs.filter(**{f"{deal_field}__in": visible_deals(user)})


def get_visible_deal_or_404(user, pk, *select_related):
    """``get_object_or_404`` over :func:`visible_deals`, so a deal the user
    can't see is indistinguishable from one that doesn't exist."""
    qs = visible_deals(user)
    if select_related:
        qs = qs.select_related(*select_related)
    return get_object_or_404(qs, pk=pk)
