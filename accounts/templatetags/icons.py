"""`{% icon "name" %}` — inline a Lucide SVG, bundled via lucide-static (npm).

Usage:
    {% load icons %}
    {% icon "contact" %}
    {% icon "building" class="h-6 w-6" %}

The SVG file is read from node_modules/lucide-static/icons and cached in
memory. Stroke uses currentColor so DaisyUI text colours apply directly.
"""

from __future__ import annotations

import functools
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

_ICON_DIR: Path = settings.BASE_DIR / "node_modules" / "lucide-static" / "icons"


@functools.lru_cache(maxsize=256)
def _read_icon(name: str) -> str:
    path = _ICON_DIR / f"{name}.svg"
    return path.read_text(encoding="utf-8").strip()


@register.simple_tag(name="icon")
def icon(name: str, **attrs: str) -> SafeString:
    """Render a Lucide icon. `class` is appended to the SVG's existing classes."""
    extra_class = attrs.pop("class", "h-5 w-5")
    extra_attrs = "".join(f' {k}="{v}"' for k, v in attrs.items())
    svg = _read_icon(name)

    # Lucide SVGs always start with `class="lucide lucide-<name>"`.
    svg = svg.replace('class="lucide ', f'class="lucide {extra_class} ', 1)
    if extra_attrs:
        svg = svg.replace("<svg", f"<svg{extra_attrs}", 1)
    return mark_safe(svg)
