from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts.views import ConsumeMagicLinkView, DashboardView

urlpatterns = [
    path("admin/", admin.site.urls),

    # Auth: login/logout/password change/reset
    path("accounts/", include("django.contrib.auth.urls")),

    # Magic-link consume — short URL, easy to send in emails
    path("m/<str:token>/", ConsumeMagicLinkView.as_view(), name="consume_magic_link"),

    path("", DashboardView.as_view(), name="dashboard"),
    path("", include("crm.urls")),
]
