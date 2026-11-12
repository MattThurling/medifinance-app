"""Customer-provided organisation details and their staff review.

The portal lets the lead applicant create their organisation and maintain
its details. What they enter is applied in one of three ways:

* **Their own unverified organisation** — everything saves straight away.
  The organisation carries ``submitted_by`` and stays *unverified* until a
  staff member verifies it; that verification is the review.
* **Contact details on any other organisation** (address, phone, email,
  website) — saved straight away and queued as an *applied*
  ``OrganisationChange`` that staff acknowledge or revert.
* **Identity fields on any other organisation** (name, trading name,
  Companies House number) — NOT saved. Held as a *proposal*
  ``OrganisationChange`` and applied only when staff approve it.

"Their own" means ``submitted_by`` is the editing user: an unverified
organisation that staff have attached to a second deal cannot be renamed by
that deal's customer.

This module is the only writer of the review state. It has no request
dependency (like ``crm.applicants``) so the portal views, the staff views
and the tests all go through the same rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone

from .access import visible_deals, visible_organisations
from .models import Deal, Note, Organisation, OrganisationChange, Stage
from .portal_checklist import deal_people

# Which bucket a field falls in is decided here, never from posted data.
IDENTITY_FIELDS = ("name", "trading_name", "companies_house_number")
CONTACT_FIELDS = (
    "address_line1",
    "address_line2",
    "address_city",
    "address_county",
    "address_postcode",
    "phone",
    "email",
    "url",
)
CUSTOMER_FIELDS = IDENTITY_FIELDS + CONTACT_FIELDS

_LABELS = {
    "name": "Name",
    "trading_name": "Trading name",
    "companies_house_number": "Companies House number",
    "address_line1": "Address line 1",
    "address_line2": "Address line 2",
    "address_city": "City",
    "address_county": "County",
    "address_postcode": "Postcode",
    "phone": "Phone",
    "email": "Email",
    "url": "Website",
}

NAME_MATCH_THRESHOLD = 0.85
_NAME_PREFILTER_LIMIT = 200

_CH_NUMBER_RE = re.compile(r"^(\d{8}|[A-Z]{2}\d{6})$")


class AlreadyHasOrganisation(Exception):
    """The deal already has an organisation (double submit, or staff set one)."""


class SwapNotAllowed(Exception):
    """The duplicate can't be replaced by the chosen organisation; ``str()``
    is a sentence fit to show staff."""


def field_label(name: str) -> str:
    return _LABELS.get(name, name.replace("_", " ").capitalize())


def normalise_ch_number(raw: str | None) -> str:
    """Canonical Companies House number: no spaces, upper case, all-digit
    numbers zero-padded to 8. Anything else comes back merely tidied — use
    ``is_valid_ch_number`` to judge it. Staff-entered numbers are free text,
    so comparisons normalise both sides."""
    value = re.sub(r"\s+", "", raw or "").upper()
    if value.isdigit() and len(value) < 8:
        value = value.zfill(8)
    return value


def is_valid_ch_number(value: str) -> bool:
    return bool(_CH_NUMBER_RE.match(value))


def normalise_name(raw: str | None) -> str:
    """Company name reduced for comparison: lower case, legal suffixes and
    punctuation dropped. Same rule as the HubSpot migration's matcher
    (``hubspot-migration/automate_deal_orgs.py``), copied because that
    tooling lives outside the app."""
    if not raw:
        return ""
    s = raw.lower().strip()
    s = re.sub(
        r"\b(ltd\.?|limited|plc|llp|llc|inc\.?|corp(oration)?|the|t/a|trading as|dba)\b",
        " ", s,
    )
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --- Customer writes --------------------------------------------------------

def create_customer_organisation(deal: Deal, cleaned_data: dict, *, by_user) -> Organisation:
    """Create the organisation a customer typed in and link it to ``deal``.

    It is live at once (so they can add co-applicants and clear their
    checklist) but unverified. Everyone on the deal joins it, matching what
    ``crm.applicants.add_applicant`` does for people added afterwards. The
    deal row is locked and re-checked so a double submit makes one
    organisation; the loser raises ``AlreadyHasOrganisation``."""
    with transaction.atomic():
        locked = Deal.objects.select_for_update().get(pk=deal.pk)
        if locked.organisation_id is not None:
            raise AlreadyHasOrganisation
        org = Organisation.objects.create(
            owner=locked.owner,
            submitted_by=by_user,
            **{name: cleaned_data.get(name) or "" for name in CUSTOMER_FIELDS},
        )
        locked.organisation = org
        locked.save(update_fields=["organisation", "updated_at"])
        for person in deal_people(locked):
            if person is not None:
                person.organisations.add(org)
        Note.objects.create(
            type=Note.Type.CUSTOMER_UPDATE,
            author=by_user,
            datetime=timezone.now(),
            content=f"{by_user.full_name} added their company. It needs verifying.",
            deal=locked,
            organisation=org,
        )
    deal.organisation = org
    return org


@dataclass
class EditResult:
    organisation: Organisation
    applied: list[str] = field(default_factory=list)  # field names saved now
    held: list[str] = field(default_factory=list)  # field names proposed, not saved

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.held)


def may_edit_freely(org: Organisation, user) -> bool:
    """The submitter of a still-unverified organisation edits every field
    with no review rows — verifying the organisation is the review."""
    return org.is_unverified and org.submitted_by_id == user.pk


def _open_row(org: Organisation, user, *, applied: bool) -> OrganisationChange:
    """The user's pending row of this kind on ``org`` (unsaved if none)."""
    row = OrganisationChange.objects.filter(
        organisation=org, submitted_by=user, applied=applied,
        status=OrganisationChange.Status.PENDING,
    ).first()
    if row is None:
        row = OrganisationChange(organisation=org, submitted_by=user, applied=applied, changes={})
    return row


def _store_row(row: OrganisationChange) -> None:
    """Save a row, or drop it when nothing is left in it."""
    if row.changes:
        row.save()
    elif row.pk:
        row.delete()


def record_customer_edit(org_pk: int, cleaned_data: dict, *, by_user) -> EditResult:
    """Apply a customer's edit of an organisation under the rules in the
    module docstring, and log it.

    Takes cleaned data, not a form: ``ModelForm.save()`` would write the held
    identity fields. The organisation is re-read under a lock and the mode
    decided from that row, so a staff verification that lands mid-edit is
    honoured. Returns the fresh organisation — render from it, not from the
    form's instance (which carries the unapproved values)."""
    with transaction.atomic():
        org = Organisation.objects.select_for_update().get(pk=org_pk)
        result = EditResult(organisation=org)
        submitted = {
            name: cleaned_data[name] or ""
            for name in CUSTOMER_FIELDS if name in cleaned_data
        }
        free = may_edit_freely(org, by_user)

        apply_now = {
            name: value for name, value in submitted.items()
            if (free or name in CONTACT_FIELDS) and value != getattr(org, name)
        }
        if apply_now and not free:
            row = _open_row(org, by_user, applied=True)
            for name, value in apply_now.items():
                # Keep the earliest "old" across stacked edits so a reject
                # restores what staff last saw, and forget a field the
                # customer has put back.
                old = row.changes.get(name, {}).get("old", getattr(org, name))
                if value == old:
                    row.changes.pop(name, None)
                else:
                    row.changes[name] = {"old": old, "new": value}
            _store_row(row)
        for name, value in apply_now.items():
            setattr(org, name, value)
        result.applied = list(apply_now)

        if not free:
            row = _open_row(org, by_user, applied=False)
            before = dict(row.changes)
            for name in IDENTITY_FIELDS:
                if name not in submitted:
                    continue
                current = getattr(org, name)
                if submitted[name] == current:
                    row.changes.pop(name, None)  # back to the live value: withdrawn
                else:
                    row.changes[name] = {"old": current, "new": submitted[name]}
            if row.changes != before:
                _store_row(row)
                result.held = [
                    name for name in IDENTITY_FIELDS
                    if row.changes.get(name) != before.get(name)
                ]

        if result.changed:
            # Always bump updated_at — a proposal doesn't touch the row
            # otherwise, and the portal's optimistic lock keys on it.
            org.save(update_fields=[*result.applied, "updated_at"])
            parts = []
            if result.applied:
                parts.append("updated company details: " + ", ".join(map(field_label, result.applied)))
            if result.held:
                parts.append(
                    "requested a change (awaiting review): "
                    + ", ".join(map(field_label, result.held))
                )
            Note.objects.create(
                type=Note.Type.CUSTOMER_UPDATE,
                author=by_user,
                datetime=timezone.now(),
                content=f"{by_user.full_name} {'; '.join(parts)}.",
                organisation=org,
            )
    return result


# --- Staff review -----------------------------------------------------------

def _staff_note(org: Organisation, by, content: str, *, deal=None) -> None:
    Note.objects.create(
        type=Note.Type.ADMIN_COMMENT,
        author=by,
        datetime=timezone.now(),
        content=content,
        organisation=org,
        deal=deal,
    )


def verify(org: Organisation, *, by) -> bool:
    """Mark a customer-created organisation as checked. False if it wasn't
    awaiting verification (already done, or never customer-created)."""
    with transaction.atomic():
        locked = Organisation.objects.select_for_update().get(pk=org.pk)
        if not locked.is_unverified:
            return False
        locked.verified_at = timezone.now()
        locked.verified_by = by
        # updated_at too, so a portal edit form opened before this conflicts
        # instead of saving under the old (free-edit) rules.
        locked.save(update_fields=["verified_at", "verified_by", "updated_at"])
        _staff_note(locked, by, "Verified the organisation details the customer entered.")
    org.verified_at, org.verified_by = locked.verified_at, by
    return True


def _close(change: OrganisationChange, status: str, by) -> None:
    change.status = status
    change.reviewed_by = by
    change.reviewed_at = timezone.now()
    change.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])


def approve(change: OrganisationChange, *, by) -> bool:
    """Accept a pending change: a proposal is applied now, an already-applied
    edit is simply acknowledged. False if it was no longer pending."""
    with transaction.atomic():
        org = Organisation.objects.select_for_update().get(pk=change.organisation_id)
        change = OrganisationChange.objects.select_for_update().get(pk=change.pk)
        if not change.is_pending:
            return False
        labels = ", ".join(field_label(name) for name in change.changes)
        if not change.applied:
            for name, values in change.changes.items():
                if name in CUSTOMER_FIELDS:
                    setattr(org, name, values["new"])
            org.save(update_fields=[
                *[n for n in change.changes if n in CUSTOMER_FIELDS], "updated_at",
            ])
        _close(change, OrganisationChange.Status.APPROVED, by)
        _staff_note(org, by, f"Approved the customer's change to: {labels}.")
    return True


def reject(change: OrganisationChange, *, by) -> bool:
    """Turn down a pending change. A proposal is discarded; an applied edit
    is reverted, field by field, but only where the organisation still holds
    the customer's value (a later staff edit wins). False if not pending."""
    with transaction.atomic():
        org = Organisation.objects.select_for_update().get(pk=change.organisation_id)
        change = OrganisationChange.objects.select_for_update().get(pk=change.pk)
        if not change.is_pending:
            return False
        labels = ", ".join(field_label(name) for name in change.changes)
        if change.applied:
            reverted = [
                name for name, values in change.changes.items()
                if name in CUSTOMER_FIELDS and getattr(org, name) == values["new"]
            ]
            for name in reverted:
                setattr(org, name, change.changes[name]["old"])
            org.save(update_fields=[*reverted, "updated_at"])
        _close(change, OrganisationChange.Status.REJECTED, by)
        _staff_note(org, by, f"Rejected the customer's change to: {labels}.")
    return True


def pending_changes(org: Organisation):
    return org.changes.filter(status=OrganisationChange.Status.PENDING)


def needs_review(user):
    """Organisations ``user`` may see that are waiting on staff: unverified,
    or carrying a pending change."""
    return visible_organisations(user).filter(
        Q(submitted_by__isnull=False, verified_at__isnull=True)
        | Q(pk__in=OrganisationChange.objects.filter(
            status=OrganisationChange.Status.PENDING,
        ).values("organisation_id"))
    )


# --- Duplicates -------------------------------------------------------------

def likely_duplicates(org: Organisation, limit: int | None = 5) -> list[Organisation]:
    """Existing organisations that look like the same business as ``org``:
    the same Companies House number, or a closely similar name. Each carries
    a ``match_reason`` for display. Number matches come first.

    Unscoped, like the organisation picker: the point is to stop a second
    record being created, whoever owns the first. A name match is only a
    suggestion — sole traders have no number to confirm it."""
    found: dict[int, Organisation] = {}
    others = Organisation.objects.exclude(pk=org.pk)

    number = normalise_ch_number(org.companies_house_number)
    if number:
        stripped = number.lstrip("0") or number
        candidates = others.filter(
            Q(companies_house_number__icontains=stripped)
        )[:_NAME_PREFILTER_LIMIT]
        for other in candidates:
            if normalise_ch_number(other.companies_house_number) == number:
                other.match_reason = "Same Companies House number"
                found[other.pk] = other

    mine = {n for n in (normalise_name(org.name), normalise_name(org.trading_name)) if n}
    tokens = sorted({t for n in mine for t in n.split() if len(t) >= 4}, key=lambda t: (-len(t), t))
    if tokens:
        token = tokens[0]
        candidates = others.filter(
            Q(name__icontains=token) | Q(legal_name__icontains=token) | Q(trading_name__icontains=token)
        )[:_NAME_PREFILTER_LIMIT]
        for other in candidates:
            if other.pk in found:
                continue
            theirs = {
                n for n in map(normalise_name, (other.name, other.legal_name, other.trading_name)) if n
            }
            if any(
                SequenceMatcher(None, a, b).ratio() >= NAME_MATCH_THRESHOLD
                for a in mine for b in theirs
            ):
                other.match_reason = "Similar name"
                found[other.pk] = other

    matches = list(found.values())
    return matches if limit is None else matches[:limit]


# Relations a swap carries across to the surviving organisation. Anything
# else pointing at the duplicate means it is in real use and must stay.
_SWAP_MOVES = ((Deal, "organisation"), (Note, "organisation"), (Stage, "organisation"))


def swap_blocker(org: Organisation) -> str | None:
    """Why ``org`` can't be swapped for an existing record, or ``None``."""
    if not org.is_unverified:
        return "Only an unverified organisation that a customer created can be replaced."
    for rel in Organisation._meta.related_objects:
        if rel.many_to_many or rel.related_model is OrganisationChange:
            continue
        if (rel.related_model, rel.field.name) in _SWAP_MOVES:
            continue
        if rel.related_model._default_manager.filter(**{rel.field.name: org.pk}).exists():
            what = rel.related_model._meta.verbose_name_plural
            return f"This organisation is already used elsewhere in the CRM ({what}), so it can't be replaced."
    return None


def swap_to_existing(org: Organisation, target: Organisation, *, by) -> Organisation:
    """Replace the customer-created ``org`` with the existing ``target``.

    Deals, notes and stage events move to ``target``; the duplicate's
    contacts join it; contact details the customer entered that differ are
    left on ``target`` as a pending proposal (nothing on the existing record
    changes until staff approve it); then the duplicate is deleted.

    ``target`` must be one of ``likely_duplicates(org)`` — checked here, at
    write time, so a forged POST can't move a deal onto an arbitrary record.
    Raises ``SwapNotAllowed``."""
    if org.pk == target.pk:
        raise SwapNotAllowed("Choose a different organisation.")
    with transaction.atomic():
        locked = {
            o.pk: o for o in
            Organisation.objects.select_for_update().filter(pk__in=[org.pk, target.pk]).order_by("pk")
        }
        if len(locked) != 2:
            raise SwapNotAllowed("That organisation no longer exists.")
        org, target = locked[org.pk], locked[target.pk]
        blocker = swap_blocker(org)
        if blocker:
            raise SwapNotAllowed(blocker)
        if target.pk not in {o.pk for o in likely_duplicates(org, limit=None)}:
            raise SwapNotAllowed("That organisation isn't a likely match for this one.")

        submitter = org.submitted_by
        deals = list(Deal.objects.filter(organisation=org))
        Deal.objects.filter(organisation=org).update(organisation=target)
        Note.objects.filter(organisation=org).update(organisation=target)
        Stage.objects.filter(organisation=org).update(organisation=target)
        target.contacts.add(*org.contacts.all())

        row = _open_row(target, submitter, applied=False)
        for name in CONTACT_FIELDS:
            theirs = getattr(org, name)
            if theirs and theirs != getattr(target, name):
                row.changes[name] = {"old": getattr(target, name), "new": theirs}
        _store_row(row)

        content = (
            f"Replaced the customer-entered organisation “{org.name}” with this existing record."
        )
        if deals:
            for deal in deals:
                _staff_note(target, by, content, deal=deal)
        else:
            _staff_note(target, by, content)
        # Keep open portal forms on the target honest about the new proposal.
        target.save(update_fields=["updated_at"])
        try:
            org.delete()
        except ProtectedError as exc:  # backstop for a relation added later
            raise SwapNotAllowed(
                "This organisation is already used elsewhere in the CRM, so it can't be replaced."
            ) from exc
    return target


# --- Template context -------------------------------------------------------

def _rows(change: OrganisationChange, org: Organisation) -> list[dict]:
    """One display row per changed field. ``current`` is the live value —
    shown instead of the stored ``old``, which may be out of date."""
    return [
        {
            "label": field_label(name),
            "old": values.get("old", ""),
            "new": values.get("new", ""),
            "current": getattr(org, name, ""),
        }
        for name, values in change.changes.items()
    ]


def review_context(org: Organisation | None, user) -> dict | None:
    """Everything the staff review panel (``crm/_organisation_review.html``)
    shows for ``org``, or ``None`` when there is nothing to review."""
    if org is None:
        return None
    changes = list(pending_changes(org).select_related("submitted_by"))
    if not org.is_unverified and not changes:
        return None
    for change in changes:
        change.rows = _rows(change, org)
    unverified = org.is_unverified
    deals = list(visible_deals(user).filter(organisation=org)) if unverified else []
    return {
        "org": org,
        "unverified": unverified,
        "changes": changes,
        "deals": deals,
        "unlinked": unverified and not Deal.objects.filter(organisation=org).exists(),
        "duplicates": likely_duplicates(org) if unverified else [],
        "swap_blocker": swap_blocker(org) if unverified else None,
    }


def portal_state(org: Organisation, user) -> dict:
    """What the portal company card shows ``user`` about review. A held
    proposal is shown only to the person who made it — another customer of
    the same organisation never sees someone else's requested name."""
    mine = pending_changes(org).filter(submitted_by=user)
    proposal = next((c for c in mine if not c.applied), None)
    return {
        "awaiting_review": org.is_unverified or bool(list(mine)),
        "proposal_rows": _rows(proposal, org) if proposal else [],
        "proposal_values": (
            {name: values["new"] for name, values in proposal.changes.items()}
            if proposal else {}
        ),
        "edits_freely": may_edit_freely(org, user),
    }
