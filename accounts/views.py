from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from .models import MagicLink


def _client_ip(request) -> str | None:
    """Best-effort client IP — honours X-Forwarded-For when behind a proxy."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR")


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        if user.is_customer:
            # OneToOne reverse accessor — may not exist if the user has no linked Contact yet.
            contact = getattr(user, "contact", None)
            ctx["customer_deals"] = contact.deals.all() if contact else []
        return ctx


class ConsumeMagicLinkView(View):
    """Validate the token, mark it used, log the user in, redirect to the target."""

    def get(self, request, token: str):
        link = get_object_or_404(MagicLink, token=token)
        if not link.is_valid:
            return render(
                request,
                "portal/link_invalid.html",
                {"link": link},
                status=410,
            )
        link.consume(ip=_client_ip(request))
        # We have multiple AUTHENTICATION_BACKENDS (ModelBackend + guardian) so
        # `login()` needs to know which one to record on the session.
        login(request, link.user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect(link.redirect_url)
