from django import forms

from ..models import Proposal
from .base import DaisyUIFormMixin


class ProposalForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: create / edit a Proposal on a Deal. `deal` is set by the view."""

    class Meta:
        model = Proposal
        fields = ["lender", "contact", "proposal_number", "status"]
