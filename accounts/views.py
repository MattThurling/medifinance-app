from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from .models import MagicLink, SiteSettings


def _client_ip(request) -> str | None:
    """Best-effort client IP — honours X-Forwarded-For when behind a proxy."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR")


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        if user.is_admin or user.is_associate:
            # Inline import — accounts is loaded before crm, so a module-level
            # import here would risk an apps-not-ready loop.
            from crm.models import Contact, Deal, Organisation
            ctx["contacts_count"] = Contact.objects.count()
            ctx["organisations_count"] = Organisation.objects.count()
            ctx["deals_count"] = Deal.objects.count()
            ctx["api_enabled"] = SiteSettings.get().api_enabled
        elif user.is_customer:
            # OneToOne reverse accessor — may not exist if the user has no linked Contact yet.
            contact = getattr(user, "contact", None)
            ctx["customer_deals"] = contact.deals.all() if contact else []
        return ctx


class ToggleApiAccessView(LoginRequiredMixin, View):
    """Flip the global `SiteSettings.api_enabled` kill switch. Admin-only."""

    def post(self, request):
        if not request.user.is_admin:
            raise PermissionDenied
        settings_row = SiteSettings.get()
        settings_row.api_enabled = not settings_row.api_enabled
        settings_row.save(update_fields=["api_enabled", "updated_at"])
        if settings_row.api_enabled:
            messages.success(request, "Partner API access turned ON.")
        else:
            messages.warning(
                request,
                "Partner API access turned OFF. New deal-create requests "
                "will be rejected until you turn it back on.",
            )
        return redirect("dashboard")


class DeveloperHomeView(TemplateView):
    """Public docs + try-it widget for the quote API. No auth on the page —
    integrators read it before they have a key."""

    template_name = "developers/index.html"


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
