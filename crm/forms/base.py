import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Role

from ..access import owner_choices


def _parse_amount(raw: str) -> "int | None":
    """'£14,999' / '1000' -> int; blank/non-numeric -> None."""
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def _parse_yield(raw: str) -> "Decimal | None":
    """'15.65%' / '7.75' -> Decimal(2dp); blank/non-numeric/out-of-range -> None."""
    s = (raw or "").replace("%", "").replace(",", "").strip()
    if not s:
        return None
    try:
        value = Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    # yield_percent is DecimalField(max_digits=5, decimal_places=2) -> < 1000.
    if value < 0 or value >= 1000:
        return None
    return value


class DaisyUIFormMixin:
    """Inject DaisyUI input/select/textarea classes onto every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            existing = w.attrs.get("class", "")
            if isinstance(w, (forms.Select, forms.SelectMultiple)):
                base = "select select-bordered w-full"
            elif isinstance(w, forms.Textarea):
                base = "textarea textarea-bordered w-full"
            elif isinstance(w, forms.CheckboxInput):
                base = "checkbox"
            else:
                base = "input input-bordered w-full"
            w.attrs["class"] = f"{existing} {base}".strip()


class OwnerInitialFormMixin:
    """Owner handling shared by the Deal, Contact and Organisation forms.

    * Admin: the owner combobox is offered (limited to staff users, never
      customers) and defaults to themselves on create.
    * Associate with an organisation: the combobox is offered too, limited
      to their organisation's staff (``crm.access.owner_choices``), and
      defaults to themselves on create.
    * Associate without one: the field is removed. New records are owned by
      the associate; on edit the existing owner is left untouched. Because
      the field isn't on the form, a forged ``owner=<pk>`` in the POST is
      ignored by ModelForm, so an associate can't hand a record away.
    """

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.fields.get("owner")
        if owner is None:
            return
        # Role filter only (no is_active): re-saving a record owned by a
        # deactivated colleague must not trip validation.
        if current_user is None:
            owner.queryset = get_user_model().objects.filter(
                role__in=[Role.ADMIN, Role.ASSOCIATE]
            )
            return
        if current_user.is_associate and not current_user.organisation_id:
            del self.fields["owner"]
            if not self.instance.pk:
                self.instance.owner = current_user
            return
        choices = owner_choices(current_user)
        if self.instance.pk and self.instance.owner_id:
            # Keep the current owner selectable even if they have since left
            # the organisation, so an unrelated edit still validates.
            choices = get_user_model().objects.filter(
                Q(pk__in=choices.values("pk")) | Q(pk=self.instance.owner_id)
            )
        owner.queryset = choices
        if not self.instance.pk:
            owner.initial = current_user


ADDRESS_FIELDS_ORG = [
    "address_line1",
    "address_line2",
    "address_city",
    "address_county",
    "address_postcode",
]

ADDRESS_FIELDS_HOME = [
    "home_address_line1",
    "home_address_line2",
    "home_address_city",
    "home_address_county",
    "home_address_postcode",
]
