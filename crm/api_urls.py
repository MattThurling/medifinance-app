"""URL routes for the JSON APIs, mounted under /api/ in the project urls.

Two surfaces:
- /api/deals/         — GET: internal extension API (session auth);
                        POST: public deal-create API (bearer-token auth)
- /api/quotes/        — public quote API (bearer-token auth)
"""

from django.urls import path

from . import api

app_name = "crm_api"

urlpatterns = [
    path("deals/", api.DealRootApi.as_view(), name="deal_list"),
    path("deals/<int:pk>/", api.DealDetailApi.as_view(), name="deal_detail"),
    path("quotes/", api.QuoteApi.as_view(), name="quote"),
]
