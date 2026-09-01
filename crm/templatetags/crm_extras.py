"""CRM template helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import nh3
from django import template
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt

from crm.models import Stage, XeroConnection

register = template.Library()

# CommonMark + newlines-as-<br> + bare URLs auto-linked. Notes are written in
# plain textareas (and converted from HubSpot HTML at import), so soft breaks
# should be visible breaks.
_md = MarkdownIt("commonmark", {"breaks": True, "linkify": True}).enable("linkify")


@register.filter(name="markdown")
def markdown(text: str | None) -> SafeString:
    """Render Markdown to sanitized HTML (used for note content).

    nh3 strips anything unsafe (scripts, event handlers, unknown tags) and
    stamps links with the DaisyUI `link` class + target/rel so they open in
    a new tab.
    """
    html = _md.render(text or "")
    return mark_safe(
        nh3.clean(html, set_tag_attribute_values={"a": {"class": "link", "target": "_blank"}})
    )


@register.simple_tag
def xero_is_connected() -> bool:
    """Whether this environment holds a Xero connection. Cheap existence check
    for the nav's sync button — only evaluated for finance users."""
    return XeroConnection.objects.exists()


@register.filter(name="stage_display")
def stage_display(code: str | None) -> str:
    """Convert a stage code (e.g. 'info_received') to its display label.

    Useful on list pages where we annotate the raw code via a Subquery and
    can't use Django's auto-generated `get_FOO_display` (which is for fields).
    """
    if not code:
        return ""
    try:
        return Stage.Name(code).label
    except ValueError:
        return code


@register.filter(name="money")
def money(value, places: int = 2) -> str:
    """Format a Decimal/number as pounds: ``£12,345.67``. ``None``/empty
    renders as an em-dash so "no data" is distinguishable from zero."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    return f"£{amount:,.{int(places)}f}"
