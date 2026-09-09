from django import forms

from ..models import Contact, Organisation
from .base import ADDRESS_FIELDS_HOME, DaisyUIFormMixin, OwnerInitialFormMixin


class ContactForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["title", "first_name", "last_name", "email", "phone", "organisations", "owner"]

    def selected_organisations(self):
        """Orgs to render as chips on the form. On a bound (POSTed) form we
        rebuild from `data` so an invalid re-post preserves the user's picks;
        otherwise we read the saved instance's M2M, falling back to whatever
        the view supplied as `initial['organisations']` (e.g. when creating
        from an org or deal page)."""
        if self.is_bound and hasattr(self.data, "getlist"):
            ids = [pk for pk in self.data.getlist("organisations") if pk]
            if ids:
                return list(Organisation.objects.filter(pk__in=ids))
            return []
        if self.instance.pk:
            return list(self.instance.organisations.all())
        initial = self.initial.get("organisations") or self.fields["organisations"].initial
        if initial:
            pks = [o.pk if hasattr(o, "pk") else o for o in initial]
            return list(Organisation.objects.filter(pk__in=pks))
        return []


class CustomerInfoForm(DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "date_of_birth", "phone"] + ADDRESS_FIELDS_HOME
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


class CoApplicantForm(DaisyUIFormMixin, forms.ModelForm):
    """One form per co-applicant in the formset. `organisation` is set by the
    view from the deal's customer's org — not in the form."""

    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone", "date_of_birth"] + ADDRESS_FIELDS_HOME
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
