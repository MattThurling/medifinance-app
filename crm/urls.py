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

    # Deals
    path("deals/", views.DealListView.as_view(), name="deal_list"),
    path("deals/new/", views.DealCreateView.as_view(), name="deal_create"),
    path("deals/<int:pk>/", views.DealDetailView.as_view(), name="deal_detail"),
    path("deals/<int:pk>/edit/", views.DealUpdateView.as_view(), name="deal_update"),
    path("deals/<int:pk>/delete/", views.DealDeleteView.as_view(), name="deal_delete"),
    path("deals/<int:pk>/applicants/add/", views.DealApplicantAddView.as_view(), name="deal_applicant_add"),

    # Quotes — created from a deal via `?deal=<pk>`; edit/delete use the quote's pk
    path("quotes/new/", views.QuoteCreateView.as_view(), name="quote_create"),
    path("quotes/<int:pk>/edit/", views.QuoteUpdateView.as_view(), name="quote_update"),
    path("quotes/<int:pk>/delete/", views.QuoteDeleteView.as_view(), name="quote_delete"),

    # Stages — append-only event log per deal
    path("stages/new/", views.StageCreateView.as_view(), name="stage_create"),

    # Proposals — created from a deal via `?deal=<pk>`; edit/delete use the proposal's pk
    path("proposals/new/", views.ProposalCreateView.as_view(), name="proposal_create"),
    path("proposals/<int:pk>/edit/", views.ProposalUpdateView.as_view(), name="proposal_update"),
    path("proposals/<int:pk>/delete/", views.ProposalDeleteView.as_view(), name="proposal_delete"),

    # Participations (suppliers) — created from a deal via `?deal=<pk>`; edit/delete by pk
    path("participations/new/", views.ParticipationCreateView.as_view(), name="participation_create"),
    path("participations/<int:pk>/edit/", views.ParticipationUpdateView.as_view(), name="participation_update"),
    path("participations/<int:pk>/delete/", views.ParticipationDeleteView.as_view(), name="participation_delete"),
    path("participations/<int:pk>/request-invoice/", views.RequestParticipationInvoiceView.as_view(), name="participation_request_invoice"),

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
