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

    # Quotes — created from a deal via `?deal=<pk>`; edit/delete use the quote's pk
    path("quotes/new/", views.QuoteCreateView.as_view(), name="quote_create"),
    path("quotes/<int:pk>/edit/", views.QuoteUpdateView.as_view(), name="quote_update"),
    path("quotes/<int:pk>/delete/", views.QuoteDeleteView.as_view(), name="quote_delete"),

    # Stages — append-only event log per deal
    path("stages/new/", views.StageCreateView.as_view(), name="stage_create"),

    # Customer portal — issuance is staff-side, consumption is via /m/<token>/
    path("deals/<int:pk>/portal-link/", views.IssuePortalLinkView.as_view(), name="deal_issue_portal_link"),
    path("deals/<int:pk>/portal-link/email/", views.EmailPortalLinkView.as_view(), name="deal_email_portal_link"),
    # Step 1: pick a quote. URL path retained as /portal/deals/<pk>/ so existing
    # magic links keep working — only the view behind it has changed.
    path("portal/deals/<int:pk>/", views.PortalQuoteSelectView.as_view(), name="portal_quote_select"),
    path("portal/deals/<int:pk>/application/", views.PortalApplicationView.as_view(), name="portal_application"),
    path("portal/deals/<int:pk>/thanks/", views.PortalApplicationCompleteView.as_view(), name="portal_application_complete"),
]
