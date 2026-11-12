from django import forms

from .. import org_review
from ..models import Deal, Organisation
from .base import (
    ADDRESS_FIELDS_ORG,
    DaisyUIFormMixin,
    OptimisticLockFormMixin,
    OwnerInitialFormMixin,
)


class OrganisationForm(OwnerInitialFormMixin, DaisyUIFormMixin, forms.ModelForm):
    class Meta:
        model = Organisation
        fields = [
            "name",
            "legal_name",
            "trading_name",
            "companies_house_number",
            "address_line1",
            "address_line2",
            "address_city",
            "address_county",
            "address_postcode",
            "url",
            "email",
            "phone",
            "sector",
            "owner",
        ]
        labels = {"url": "Website"}

    def clean_sector(self):
        # Normalise the cleared select to NULL so "unset" is one state, not two.
        return self.cleaned_data.get("sector") or None


class CustomerCompanyForm(OptimisticLockFormMixin, DaisyUIFormMixin, forms.ModelForm):
    """Customer portal: the organisation fields a customer may provide.

    The legal name, sector and owner are deliberately absent — staff-only;
    ModelForms ignore posted fields not in Meta.fields, so omission IS the
    lock. Being on this form does not mean a value is saved: the portal
    views hand ``cleaned_data`` to ``crm.org_review``, which applies contact
    details at once but holds name / trading name / Companies House number
    for staff approval once the organisation is verified. Never call
    ``save()`` on an edit."""

    class Meta:
        model = Organisation
        fields = list(org_review.IDENTITY_FIELDS) + ADDRESS_FIELDS_ORG + ["phone", "email", "url"]
        labels = {"url": "Website", "name": "Company name"}
        help_texts = {
            "name": "",
            "trading_name": "Only if you trade under a different name.",
            "companies_house_number": "If the business is registered at Companies House.",
        }

    def clean_companies_house_number(self):
        raw = self.cleaned_data.get("companies_house_number") or ""
        if not raw.strip():
            return ""
        number = org_review.normalise_ch_number(raw)
        if not org_review.is_valid_ch_number(number):
            raise forms.ValidationError(
                "That doesn't look like a Companies House number — it is 8 digits, "
                "or two letters followed by 6 digits."
            )
        return number


class CustomerCompanyCreateForm(CustomerCompanyForm):
    """Customer portal: add the company to a deal that has none (partner-API
    applications start that way). Shaped by the deal's company type, which
    the applicant already gave: a sole trader has no Companies House number,
    so the field is dropped. The number is never required — partnerships
    that aren't limited have none, and the company type on a deal isn't
    reliable enough to insist on one. Staff check it when verifying."""

    def __init__(self, *args, deal=None, **kwargs):
        super().__init__(*args, **kwargs)
        company_type = getattr(deal, "company_type", "") or ""
        if company_type == Deal.CompanyType.SOLE_TRADER:
            del self.fields["companies_house_number"]
            del self.fields["trading_name"]
        if company_type in (Deal.CompanyType.SOLE_TRADER, Deal.CompanyType.PARTNERSHIP):
            self.fields["name"].label = "Business or trading name"


class QuickOrganisationForm(OrganisationForm):
    """Inline "New organisation" from a combobox (deal form, supplier form):
    the identifying fields only. No owner field — the quick-create view
    assigns the creator (see ``crm.views.shared._QuickCreateView``)."""

    class Meta(OrganisationForm.Meta):
        fields = ["name", "legal_name", "companies_house_number", "sector", "email", "phone"]
