from django import forms
from django.conf import settings

from ..models import Document
from .base import DaisyUIFormMixin


class DocumentRequestForm(DaisyUIFormMixin, forms.ModelForm):
    """Staff: request a document on a deal (the file comes later, on upload)."""

    class Meta:
        model = Document
        fields = ["name", "required"]


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Django's documented multi-file pattern: the widget accepts several
    files and `clean` validates each one, returning a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(d, initial) for d in data]
        return [single_clean(data, initial)]


class DocumentUploadForm(forms.Form):
    """Upload one or more files against an existing document request. Several
    files are zipped together by `Document.attach`."""

    file = MultipleFileField()

    def clean_file(self):
        files = self.cleaned_data.get("file") or []
        if not files:
            raise forms.ValidationError("Please choose a file to upload.")
        max_file = settings.DOCUMENT_UPLOAD_MAX_FILE_MB
        max_total = settings.DOCUMENT_UPLOAD_MAX_TOTAL_MB
        for f in files:
            if f.size > max_file * 1024 * 1024:
                raise forms.ValidationError(
                    f"“{f.name}” is too large. Each file must be under {max_file} MB."
                )
        if sum(f.size for f in files) > max_total * 1024 * 1024:
            raise forms.ValidationError(
                f"Those files add up to more than {max_total} MB. Please upload fewer at a time."
            )
        return files
