from django import forms

from ..models import Document
from .base import DaisyUIFormMixin


class DocumentRequestForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: request a document on a deal (the file comes later, on upload)."""

    class Meta:
        model = Document
        fields = ["name", "required"]


class DocumentUploadForm(forms.ModelForm):
    """Upload a file against an existing document request."""

    class Meta:
        model = Document
        fields = ["file"]

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            raise forms.ValidationError("Please choose a file to upload.")
        return f
