from django import forms

from ..models import Contact, Organisation
from .base import (
    ADDRESS_FIELDS_HOME,
    DaisyUIFormMixin,
    OptimisticLockFormMixin,
    OwnerInitialFormMixin,
)


class ContactForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    # `organisations` is deliberately unscoped: the organisation picker is
    # open to all staff (see OrganisationSearchView).
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


class CustomerInfoForm(OptimisticLockFormMixin, DaisyUIFormMixin, forms.ModelForm):
    """Customer portal: the personal details an applicant may edit inline.
    Email is deliberately absent — it is the portal login identity."""

    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "date_of_birth", "phone"] + ADDRESS_FIELDS_HOME
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


class CustomerApplicantForm(DaisyUIFormMixin, forms.ModelForm):
    """Customer portal: the primary applicant adds someone from their company
    to the application. Always creates a new Contact (see
    ``crm.applicants.add_applicant``); the organisation comes from the deal.
    Rejects an email already used by someone on this deal, so the new person
    can be issued their own portal login."""

    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone"]

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deal = deal
        for name in ("first_name", "last_name", "email"):
            self.fields[name].required = True
        self.fields["email"].help_text = "We'll email them their own secure link to this application."

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if self.deal is not None:
            on_deal = [self.deal.customer, *self.deal.co_applicants.all()]
            if any(c and c.email and c.email.lower() == email.lower() for c in on_deal):
                raise forms.ValidationError(
                    "Someone with this email address is already on this application."
                )
        return email
