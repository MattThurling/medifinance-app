from django import forms

from ..models import Contact
from .base import DaisyUIFormMixin


class SignatureRequestForm(DaisyUIFormMixin, forms.Form):
    """Send a document for e-signature via DocuSeal. Template choices are
    fetched from the DocuSeal instance at request time — template ids differ
    between environments, so they're never hardcoded."""

    template = forms.ChoiceField(label="Agreement template")
    signer = forms.ModelChoiceField(
        queryset=Contact.objects.none(),
        help_text="Which applicant signs. Their name goes on the agreement and the "
                  "request is recorded against them.",
    )
    signer_email = forms.EmailField(help_text="Where the signing link will be sent.")

    def __init__(self, *args, templates: list[dict], signers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].choices = [(t["id"], t["name"]) for t in templates]
        self.fields["signer"].queryset = signers
