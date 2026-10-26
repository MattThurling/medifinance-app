"""crm forms, one module per resource."""
from .base import (
    ADDRESS_FIELDS_HOME,
    ADDRESS_FIELDS_ORG,
    DaisyUIFormMixin,
    OptimisticLockFormMixin,
    OwnerInitialFormMixin,
)
from .organisation import CustomerCompanyForm, OrganisationForm
from .contact import ContactForm, CustomerApplicantForm, CustomerInfoForm
from .deal import DealForm
from .participation import ParticipationForm, SupplierInvoiceForm
from .quote import QuoteForm, QuoteSelectionForm, TermsForm
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
    "OptimisticLockFormMixin",
    "OwnerInitialFormMixin",
    "CustomerCompanyForm",
    "OrganisationForm",
    "ContactForm",
    "CustomerApplicantForm",
    "CustomerInfoForm",
    "DealForm",
    "ParticipationForm",
    "SupplierInvoiceForm",
    "QuoteForm",
    "QuoteSelectionForm",
    "TermsForm",
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
