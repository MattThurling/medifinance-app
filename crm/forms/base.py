import re
from decimal import Decimal, InvalidOperation

from django import forms


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
    """Defaults `owner` to the logged-in staff member on create forms."""

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_user is not None and not self.instance.pk:
            self.fields["owner"].initial = current_user


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
