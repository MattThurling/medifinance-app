"""URL routes for the extension JSON API, mounted under /api/ in the project urls."""

from django.urls import path

from . import api

app_name = "crm_api"

urlpatterns = [
    path("deals/", api.DealListApi.as_view(), name="deal_list"),
    path("deals/<int:pk>/", api.DealDetailApi.as_view(), name="deal_detail"),
]
