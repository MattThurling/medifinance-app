from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from . import two_factor
from .models import MagicLink, SiteSettings


def _client_ip(request) -> str | None:
    """Best-effort client IP — honours X-Forwarded-For when behind a proxy."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR")


class DashboardView(LoginRequiredMixin, TemplateView):
    """Staff home. Customers have no dashboard: `/` sends them to their deal
    page when they have exactly one deal, otherwise to their deals list."""

    template_name = "dashboard.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_customer:
            # Inline import — accounts is loaded before crm, so a module-level
            # import here would risk an apps-not-ready loop.
            from crm.views.customer import customer_deals
            pks = list(customer_deals(request.user).values_list("pk", flat=True)[:2])
            if len(pks) == 1:
                return redirect("crm:my_deal_detail", pk=pks[0])
            return redirect("crm:my_deals")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Inline import — see get().
        from crm import stats
        from crm.access import visible_contacts, visible_deals, visible_organisations
        # Every count is scoped to what this user may see.
        ctx["contacts_count"] = visible_contacts(self.request.user).count()
        ctx["organisations_count"] = visible_organisations(self.request.user).count()
        ctx["deals_count"] = visible_deals(self.request.user).count()
        ctx["api_enabled"] = SiteSettings.get().api_enabled
        # Headline deal stats at the default window; the Reports page has
        # the filters and the full breakdowns.
        ctx["stats"] = stats.dashboard_stats(stats.StatsParams.from_request(self.request))
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


# --- Two-factor authentication ----------------------------------------------

def _qr_svg(data: str) -> str:
    """Inline SVG QR code for the otpauth URI — no image files, no PIL."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


class TwoFactorSetupView(LoginRequiredMixin, View):
    """Enrol an authenticator app. GET shows a QR code (and the key for
    manual entry) for a secret held in the session; POST confirms the user
    scanned it by checking a code. Nothing touches the user row until the
    code is right."""

    template_name = "registration/two_factor_setup.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.two_factor_enabled:
            # Already enrolled — re-enrolling means turning it off first,
            # which needs a valid code, so a hijacked session can't swap keys.
            if not two_factor.is_verified(request):
                return redirect(reverse("two_factor_verify") + "?" + request.META.get("QUERY_STRING", ""))
            return redirect("two_factor_settings")
        return super().dispatch(request, *args, **kwargs)

    def _secret(self, request) -> str:
        secret = request.session.get(two_factor.SESSION_PENDING_SECRET_KEY)
        if not secret:
            secret = request.user.generate_totp_secret()
            request.session[two_factor.SESSION_PENDING_SECRET_KEY] = secret
        return secret

    def _render(self, request, *, error: str | None = None):
        secret = self._secret(request)
        return render(request, self.template_name, {
            "secret": secret,
            "secret_grouped": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "qr_svg": _qr_svg(request.user.totp_provisioning_uri(secret)),
            "error": error,
            "next": two_factor.safe_next(request, default=""),
            "required": request.user.two_factor_required,
        })

    def get(self, request):
        return self._render(request)

    def post(self, request):
        secret = self._secret(request)
        if request.user.confirm_totp(secret, request.POST.get("code", "")):
            two_factor.mark_verified(request)
            messages.success(request, "Two-factor authentication is now on for your account.")
            return redirect(two_factor.safe_next(request, default=reverse("dashboard")))
        if two_factor.record_failed_attempt(request):
            return redirect("login")
        return self._render(request, error="That code didn't match. Check the app and try again.")


class TwoFactorVerifyView(LoginRequiredMixin, View):
    """Second step of login for enrolled users: enter the current code."""

    template_name = "registration/two_factor_verify.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if not request.user.two_factor_enabled:
                return redirect("two_factor_setup")
            if two_factor.is_verified(request):
                return redirect(two_factor.safe_next(request, default=reverse("dashboard")))
        return super().dispatch(request, *args, **kwargs)

    def _render(self, request, *, error: str | None = None):
        return render(request, self.template_name, {
            "error": error,
            "next": two_factor.safe_next(request, default=""),
        })

    def get(self, request):
        return self._render(request)

    def post(self, request):
        if request.user.verify_totp(request.POST.get("code", "")):
            two_factor.mark_verified(request)
            return redirect(two_factor.safe_next(request, default=reverse("dashboard")))
        if two_factor.record_failed_attempt(request):
            return redirect("login")
        return self._render(request, error="That code didn't match. Codes change every 30 seconds — try the latest one.")


class TwoFactorSettingsView(LoginRequiredMixin, TemplateView):
    """Account → Security: shows whether 2FA is on, with set-up / turn-off."""

    template_name = "account/security.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["two_factor_required"] = self.request.user.two_factor_required
        ctx["two_factor_enabled"] = self.request.user.two_factor_enabled
        return ctx


class TwoFactorDisableView(LoginRequiredMixin, View):
    """Turn 2FA off for an opted-in user. Needs a current code so a stolen
    session can't strip the protection. Refused outright when the role
    requires it — only an admin can reset those (from Django admin)."""

    def post(self, request):
        if request.user.two_factor_required:
            raise PermissionDenied
        if not request.user.two_factor_enabled:
            return redirect("two_factor_settings")
        if not request.user.verify_totp(request.POST.get("code", "")):
            messages.error(request, "That code didn't match, so two-factor authentication stays on.")
            return redirect("two_factor_settings")
        request.user.reset_totp()
        request.session.pop(two_factor.SESSION_VERIFIED_KEY, None)
        messages.warning(request, "Two-factor authentication has been turned off for your account.")
        return redirect("two_factor_settings")
