from django.urls import path

from . import views

app_name = "crm"

urlpatterns = [
    # Organisations
    path("organisations/", views.OrganisationListView.as_view(), name="organisation_list"),
    path("organisations/new/", views.OrganisationCreateView.as_view(), name="organisation_create"),
    path("organisations/<int:pk>/", views.OrganisationDetailView.as_view(), name="organisation_detail"),
    path("organisations/<int:pk>/edit/", views.OrganisationUpdateView.as_view(), name="organisation_update"),
    path("organisations/<int:pk>/delete/", views.OrganisationDeleteView.as_view(), name="organisation_delete"),

    # Contacts
    path("contacts/", views.ContactListView.as_view(), name="contact_list"),
    path("contacts/new/", views.ContactCreateView.as_view(), name="contact_create"),
    path("contacts/<int:pk>/", views.ContactDetailView.as_view(), name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.ContactUpdateView.as_view(), name="contact_update"),
    path("contacts/<int:pk>/delete/", views.ContactDeleteView.as_view(), name="contact_delete"),

    # Rates — staff lookup by term + amount, plus single-band add + CSV upload
    path("rates/", views.RatesView.as_view(), name="rates"),
    path("rates/add/", views.RateBandAddView.as_view(), name="rate_band_add"),
    path("rates/upload/", views.RateUploadView.as_view(), name="rate_upload"),

    # Deals
    path("deals/", views.DealListView.as_view(), name="deal_list"),
    path("deals/maturing/", views.DealMaturingListView.as_view(), name="deal_maturing"),
    path("deals/new/", views.DealCreateView.as_view(), name="deal_create"),
    path("deals/<int:pk>/", views.DealDetailView.as_view(), name="deal_detail"),
    path("deals/<int:pk>/overview/", views.DealOverviewView.as_view(), name="deal_overview"),
    path("deals/<int:pk>/edit/", views.DealUpdateView.as_view(), name="deal_update"),
    path("deals/<int:pk>/delete/", views.DealDeleteView.as_view(), name="deal_delete"),
    path("deals/<int:pk>/applicants/add/", views.DealApplicantAddView.as_view(), name="deal_applicant_add"),
    path("deals/<int:pk>/request-commission-invoice/", views.RequestDealCommissionInvoiceView.as_view(), name="deal_request_commission_invoice"),

    # Quotes — created from a deal via `?deal=<pk>`; edit/delete use the quote's pk
    path("quotes/rate-options/", views.QuoteRateOptionsView.as_view(), name="quote_rate_options"),
    path("quotes/new/", views.QuoteCreateView.as_view(), name="quote_create"),
    path("quotes/<int:pk>/edit/", views.QuoteUpdateView.as_view(), name="quote_update"),
    path("quotes/<int:pk>/delete/", views.QuoteDeleteView.as_view(), name="quote_delete"),
    path("quotes/<int:pk>/select/", views.QuoteSelectView.as_view(), name="quote_select"),

    # Stages — append-only event log per deal
    path("stages/new/", views.StageCreateView.as_view(), name="stage_create"),

    # Notes — added from a contact / organisation / deal detail page
    path("notes/add/", views.NoteCreateView.as_view(), name="note_create"),

    # Proposals — created from a deal via `?deal=<pk>`; edit by pk. No delete: a
    # proposal can only have its status changed (Withdrawn covers 'rescinded').
    path("proposals/new/", views.ProposalCreateView.as_view(), name="proposal_create"),
    path("proposals/<int:pk>/edit/", views.ProposalUpdateView.as_view(), name="proposal_update"),
    path("proposals/<int:pk>/select/", views.ProposalSelectView.as_view(), name="proposal_select"),
    path("proposals/<int:pk>/notify/", views.ProposalNotifyClientView.as_view(), name="proposal_notify"),

    # Participations (suppliers) — created from a deal via `?deal=<pk>`; edit/delete by pk
    path("participations/new/", views.ParticipationCreateView.as_view(), name="participation_create"),
    path("participations/<int:pk>/edit/", views.ParticipationUpdateView.as_view(), name="participation_update"),
    path("participations/<int:pk>/delete/", views.ParticipationDeleteView.as_view(), name="participation_delete"),
    path("participations/<int:pk>/request-invoice/", views.RequestParticipationInvoiceView.as_view(), name="participation_request_invoice"),
    path("participations/<int:pk>/invoice/", views.ParticipationInvoiceDownloadView.as_view(), name="participation_invoice_download"),

    # Supplier invoice upload — public token-based link, mirrors the customer magic-link UX
    path("p/<str:token>/", views.SubmitParticipationInvoiceView.as_view(), name="participation_submit_invoice"),

    # HTMX combobox search endpoints (staff)
    path("search/contacts/", views.ContactSearchView.as_view(), name="contact_search"),
    path("search/organisations/", views.OrganisationSearchView.as_view(), name="organisation_search"),
    path("search/users/", views.UserSearchView.as_view(), name="user_search"),

    # Documents — staff request via `?deal=<pk>`; upload/download/delete by pk
    path("documents/new/", views.DocumentCreateView.as_view(), name="document_create"),
    path("documents/<int:pk>/upload/", views.DocumentUploadView.as_view(), name="document_upload"),
    path("documents/<int:pk>/download/", views.DocumentDownloadView.as_view(), name="document_download"),
    path("documents/<int:pk>/delete/", views.DocumentDeleteView.as_view(), name="document_delete"),

    # DocuSeal e-signing — send a deal for signature, manage the request
    path("deals/<int:pk>/sign/", views.SignatureRequestCreateView.as_view(), name="deal_sign"),
    path("signatures/<int:pk>/resend/", views.SignatureRequestResendView.as_view(), name="signature_resend"),
    path("signatures/<int:pk>/void/", views.SignatureRequestVoidView.as_view(), name="signature_void"),
    path("signatures/<int:pk>/signed/", views.SignatureSignedFileDownloadView.as_view(), name="signature_signed_download"),
    path("signatures/<int:pk>/audit/", views.SignatureAuditDownloadView.as_view(), name="signature_audit_download"),
    # Inbound webhook (secret-header auth, no session)
    path("webhooks/docuseal/", views.DocuSealWebhookView.as_view(), name="docuseal_webhook"),

    # Xero — OAuth flow + status page + raise-invoice on a deal
    path("xero/", views.XeroStatusView.as_view(), name="xero_status"),
    path("xero/connect/", views.XeroConnectView.as_view(), name="xero_connect"),
    path("xero/callback/", views.XeroCallbackView.as_view(), name="xero_callback"),
    path("xero/disconnect/", views.XeroDisconnectView.as_view(), name="xero_disconnect"),
    path("xero/sync/", views.XeroSyncInvoicesView.as_view(), name="xero_sync"),
    path("deals/<int:pk>/raise-invoice/", views.DealRaiseInvoiceView.as_view(), name="deal_raise_invoice"),

    # Customer portal — issuance is staff-side, consumption is via /m/<token>/
    path("deals/<int:pk>/portal-link/", views.IssuePortalLinkView.as_view(), name="deal_issue_portal_link"),
    path("deals/<int:pk>/portal-link/email/", views.EmailPortalLinkView.as_view(), name="deal_email_portal_link"),
    # Step 1: pick a quote. URL path retained as /portal/deals/<pk>/ so existing
    # magic links keep working — only the view behind it has changed.
    path("portal/deals/<int:pk>/", views.PortalQuoteSelectView.as_view(), name="portal_quote_select"),
    path("portal/deals/<int:pk>/company/", views.PortalCompanyView.as_view(), name="portal_company"),
    path("portal/deals/<int:pk>/applicants/", views.PortalApplicantsView.as_view(), name="portal_applicants"),
    path("portal/deals/<int:pk>/documents/", views.PortalDocumentsView.as_view(), name="portal_documents"),
    path("portal/deals/<int:pk>/thanks/", views.PortalApplicationCompleteView.as_view(), name="portal_application_complete"),
]
