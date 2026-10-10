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
        required=False,
        help_text="Which applicant this signature is recorded against.",
    )
    signer_email = forms.EmailField(help_text="Where the signing link will be sent.")
    signer_name = forms.CharField(max_length=255, required=False)
    message = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Optional note included in the email when you send the request.",
    )

    def __init__(self, *args, templates: list[dict], signers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].choices = [(t["id"], t["name"]) for t in templates]
        self.fields["signer"].queryset = signers
