"""Row-level visibility rules.

One place answers "which deals, contacts and organisations may this user
see?" so every list, detail, child-object lookup and API endpoint agrees.

Associates are scoped by organisation: a user linked to an Organisation
(``User.organisation``) shares records with every other user linked to the
same one, as a single team. An associate with no organisation sees only
their own records. Admins see everything, including unowned rows.

Contacts and organisations are softer: an associate sees records owned by
their team *or by nobody* (unowned rows are the shared pool), plus
*reachability*: anything attached to a deal the user can see (its customer,
co-applicants, organisation, introducer, suppliers, lenders, ...) is visible
too, so links from a deal page never dead-end. Only records owned by staff
outside the team are hidden.

Kept out of ``crm.views`` so ``crm.api``, ``crm.stats``, ``crm.forms`` and
``accounts.views`` can import it without pulling in the whole views package.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404

from accounts.models import Role

from .models import (
    Contact,
    Deal,
    Organisation,
    Participation,
    Proposal,
    Quote,
    SignatureRequest,
    Stage,
)


def _owned_by_team(user) -> Q:
    """Owner rule for associates, shared by deals, contacts and organisations:
    owned by anyone in the user's organisation, or by the user alone when they
    have none. Unowned rows (owner NULL) never match."""
    if user.organisation_id:
        return Q(owner__organisation_id=user.organisation_id)
    return Q(owner=user)


def visible_deals(user):
    """Deals ``user`` may see and act on.

    * admin      -> every deal, including unowned ones (owner is NULL)
    * associate  -> deals owned by anyone in their organisation, or just
                    their own when they have no organisation
    * customer   -> deals where they are the customer contact
    * anonymous / anything else -> none
    """
    if not user.is_authenticated:
        return Deal.objects.none()
    if user.is_admin:
        return Deal.objects.all()
    if user.is_associate:
        return Deal.objects.filter(_owned_by_team(user))
    if user.is_customer:
        return Deal.objects.filter(customer__user=user)
    return Deal.objects.none()


def visible_contacts(user):
    """Contacts ``user`` may see and act on.

    * admin      -> every contact
    * associate  -> contacts owned by their team (see ``_owned_by_team``) or
                    by nobody, plus any contact on a deal they can see: the
                    customer, co-applicants, proposal contacts, supplier
                    invoice contacts and signers. Contacts owned by staff
                    outside the team are hidden.
    * customer / anonymous -> none (the portal has its own lookups)
    """
    if not user.is_authenticated:
        return Contact.objects.none()
    if user.is_admin:
        return Contact.objects.all()
    if not user.is_associate:
        return Contact.objects.none()
    deals = visible_deals(user)
    # Every reachability term is a ``pk__in`` subquery, so no join on the
    # outer table and therefore no duplicate rows (no ``distinct()`` needed).
    return Contact.objects.filter(
        _owned_by_team(user)
        | Q(owner__isnull=True)
        | Q(pk__in=deals.values("customer_id"))
        | Q(pk__in=Contact.objects.filter(co_applicant_deals__in=deals).values("pk"))
        | Q(pk__in=Proposal.objects.filter(deal__in=deals).values("contact_id"))
        | Q(pk__in=Participation.objects.filter(deal__in=deals).values("invoice_contact_id"))
        | Q(pk__in=SignatureRequest.objects.filter(deal__in=deals).values("signer_id"))
    )


def visible_organisations(user):
    """Organisations ``user`` may see and act on.

    * admin      -> every organisation
    * associate  -> organisations owned by their team or by nobody, plus any
                    on a deal they can see: the customer's organisation,
                    introducer, suppliers, lenders (proposals, quotes, rate
                    bands) and stage organisations. Organisations owned by
                    staff outside the team are hidden.
    * customer / anonymous -> none
    """
    if not user.is_authenticated:
        return Organisation.objects.none()
    if user.is_admin:
        return Organisation.objects.all()
    if not user.is_associate:
        return Organisation.objects.none()
    deals = visible_deals(user)
    quotes = Quote.objects.filter(deal__in=deals)
    return Organisation.objects.filter(
        _owned_by_team(user)
        | Q(owner__isnull=True)
        | Q(pk__in=deals.values("organisation_id"))
        | Q(pk__in=deals.values("introducer_id"))
        | Q(pk__in=Participation.objects.filter(deal__in=deals).values("organisation_id"))
        | Q(pk__in=Proposal.objects.filter(deal__in=deals).values("lender_id"))
        | Q(pk__in=quotes.values("lender_id"))
        | Q(pk__in=quotes.values("rate__organisation_id"))
        # Stage.organisation has related_name="+", so a subquery is the only route.
        | Q(pk__in=Stage.objects.filter(deal__in=deals).values("organisation_id"))
    )


def owner_choices(user):
    """Staff users ``user`` may filter records by, or assign as a record's owner.

    * admin                          -> every staff user
    * associate with an organisation -> staff in that organisation
    * associate without one          -> only themselves

    Role filter only (no ``is_active``): callers that show a dropdown add it
    themselves, while forms must still validate a record owned by a
    deactivated colleague.
    """
    staff = get_user_model().objects.filter(role__in=[Role.ADMIN, Role.ASSOCIATE])
    if not user.is_authenticated:
        return staff.none()
    if user.is_admin:
        return staff
    if user.is_associate and user.organisation_id:
        return staff.filter(organisation_id=user.organisation_id)
    return staff.filter(pk=user.pk)


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
