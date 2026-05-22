"""CRM template helpers."""

from __future__ import annotations

from django import template

from crm.models import Stage

register = template.Library()


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
