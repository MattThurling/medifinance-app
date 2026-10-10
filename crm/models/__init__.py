"""crm models, one module per resource.

Import order follows the FK dependency graph (base -> organisation ->
contact -> deal -> children) — do not alphabetize.
"""
from .base import TimestampedModel, _hubspot_url
from .organisation import Organisation
from .contact import Contact
from .deal import Deal
from .participation import (
    Participation,
    ParticipationInvoiceLink,
    participation_invoice_upload_path,
)
from .proposal import Proposal
from .stage import Stage
from .quote import Quote
from .rateband import RateBand, RateBandQuerySet
from .xero import XeroConnection, XeroInvoice
from .document import Document, document_upload_path
from .signature import (
    SignatureRequest,
    signature_audit_upload_path,
    signature_signed_upload_path,
)
from .note import Note
from .api_request import PartnerApiRequest

__all__ = [
    "TimestampedModel",
    "_hubspot_url",
    "Organisation",
    "Contact",
    "Deal",
    "Participation",
    "ParticipationInvoiceLink",
    "participation_invoice_upload_path",
    "Proposal",
    "Stage",
    "Quote",
    "RateBand",
    "RateBandQuerySet",
    "XeroConnection",
    "XeroInvoice",
    "Document",
    "document_upload_path",
    "SignatureRequest",
    "signature_audit_upload_path",
    "signature_signed_upload_path",
    "Note",
    "PartnerApiRequest",
]
