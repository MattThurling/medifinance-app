"""crm forms, one module per resource."""
from .base import (
    ADDRESS_FIELDS_HOME,
    ADDRESS_FIELDS_ORG,
    DaisyUIFormMixin,
    OwnerInitialFormMixin,
)
from .organisation import CustomerCompanyForm, OrganisationForm
from .contact import CoApplicantForm, ContactForm, CustomerInfoForm
from .deal import DealForm
from .participation import ParticipationForm, SupplierInvoiceForm
from .quote import CommercialQuoteForm, QuoteForm, QuoteSelectionForm
from .stage import StageForm
from .proposal import ProposalForm
from .document import DocumentRequestForm, DocumentUploadForm
from .signature import SignatureRequestForm
from .xero import XeroInvoiceForm
from .rateband import RateBandForm, RateLookupForm, RateUploadForm

__all__ = [
    "ADDRESS_FIELDS_HOME",
    "ADDRESS_FIELDS_ORG",
    "DaisyUIFormMixin",
    "OwnerInitialFormMixin",
    "CustomerCompanyForm",
    "OrganisationForm",
    "CoApplicantForm",
    "ContactForm",
    "CustomerInfoForm",
    "DealForm",
    "ParticipationForm",
    "SupplierInvoiceForm",
    "CommercialQuoteForm",
    "QuoteForm",
    "QuoteSelectionForm",
    "StageForm",
    "ProposalForm",
    "DocumentRequestForm",
    "DocumentUploadForm",
    "SignatureRequestForm",
    "XeroInvoiceForm",
    "RateBandForm",
    "RateLookupForm",
    "RateUploadForm",
]
