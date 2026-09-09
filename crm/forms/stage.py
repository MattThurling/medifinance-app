from django import forms

from ..models import Stage
from .base import DaisyUIFormMixin


class StageForm(DaisyUIFormMixin, forms.ModelForm):
    """Stage-change ModelForm. `deal`, `set_by` and `occurred_at` are set by the view."""

    class Meta:
        model = Stage
        fields = ["name", "note"]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 3}),
        }
