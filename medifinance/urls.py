from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts.views import (
    ConsumeMagicLinkView,
    DashboardView,
    DeveloperHomeView,
    ToggleApiAccessView,
    TwoFactorDisableView,
    TwoFactorSettingsView,
    TwoFactorSetupView,
    TwoFactorVerifyView,
)

urlpatterns = [
    path("admin/", admin.site.urls),

    # Auth: login/logout/password change/reset
    path("accounts/", include("django.contrib.auth.urls")),

    # Two-factor authentication (TOTP). Roles forced to use it come from
    # TWO_FACTOR_REQUIRED_ROLES; the middleware in accounts.two_factor gates.
    path("accounts/2fa/setup/", TwoFactorSetupView.as_view(), name="two_factor_setup"),
    path("accounts/2fa/verify/", TwoFactorVerifyView.as_view(), name="two_factor_verify"),
    path("accounts/security/", TwoFactorSettingsView.as_view(), name="two_factor_settings"),
    path("accounts/2fa/disable/", TwoFactorDisableView.as_view(), name="two_factor_disable"),

    # Magic-link consume — short URL, easy to send in emails
    path("m/<str:token>/", ConsumeMagicLinkView.as_view(), name="consume_magic_link"),

    # JSON API for the browser extension (staff session auth)
    path("api/", include("crm.api_urls")),

    # Public developer docs + try-it widget for the quote API
    path("developers/", DeveloperHomeView.as_view(), name="developers"),

    path("", DashboardView.as_view(), name="dashboard"),
    path("settings/api-access/toggle/", ToggleApiAccessView.as_view(), name="toggle_api_access"),
    path("", include("crm.urls")),
]
