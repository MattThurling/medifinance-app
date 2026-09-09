from decimal import Decimal

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from accounts.permissions import FinanceRequiredMixin

from ..models import XeroConnection, XeroInvoice


class XeroStatusView(FinanceRequiredMixin, TemplateView):
    """Shows whether the CRM is connected to Xero + the Connect / Disconnect controls."""

    template_name = "crm/xero_status.html"

    def get_context_data(self, **kwargs):
        from .. import xero as xero_helpers
        ctx = super().get_context_data(**kwargs)
        ctx["connection"] = XeroConnection.objects.first()
        ctx["xero_configured"] = xero_helpers.is_configured()
        return ctx


class XeroConnectView(FinanceRequiredMixin, View):
    """Kick off the OAuth dance — redirects to Xero's authorize URL with a CSRF state."""

    def get(self, request):
        import secrets as _secrets
        from .. import xero as xero_helpers

        if not xero_helpers.is_configured():
            messages.error(
                request,
                "Xero developer credentials aren't set on this environment yet.",
            )
            return redirect("crm:xero_status")

        state = _secrets.token_urlsafe(16)
        request.session["xero_oauth_state"] = state
        redirect_uri = request.build_absolute_uri(reverse("crm:xero_callback"))
        return redirect(xero_helpers.get_authorize_url(redirect_uri, state))


class XeroCallbackView(FinanceRequiredMixin, View):
    """Xero redirects back here with a one-shot code — exchange it for tokens
    and store the connected org."""

    def get(self, request):
        from datetime import timedelta
        from .. import xero as xero_helpers
        from django.utils import timezone

        code = request.GET.get("code")
        state = request.GET.get("state")
        expected_state = request.session.pop("xero_oauth_state", None)
        if not code or state != expected_state:
            messages.error(request, "Xero authorisation failed (state mismatch).")
            return redirect("crm:xero_status")

        redirect_uri = request.build_absolute_uri(reverse("crm:xero_callback"))
        try:
            tokens = xero_helpers.exchange_code_for_tokens(code, redirect_uri)
            tenants = xero_helpers.list_authorised_tenants(tokens["access_token"])
        except Exception as exc:
            messages.error(request, f"Couldn't talk to Xero: {exc}")
            return redirect("crm:xero_status")

        if not tenants:
            messages.error(request, "Xero didn't return any organisations for this account.")
            return redirect("crm:xero_status")

        # A grant can carry several orgs (e.g. authorised at different times) —
        # the one just approved is the newest, so don't blindly take the first.
        tenants.sort(key=lambda t: t.get("createdDateUtc") or "", reverse=True)
        tenant = tenants[0]
        if len(tenants) > 1:
            messages.info(
                request,
                f"Your Xero login has {len(tenants)} organisations authorised "
                f"for this app — using the most recently authorised.",
            )
        XeroConnection.objects.all().delete()  # only one connection at a time
        XeroConnection.objects.create(
            tenant_id=tenant["tenantId"],
            tenant_name=tenant.get("tenantName") or tenant.get("tenantType") or "Xero org",
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=timezone.now() + timedelta(seconds=int(tokens.get("expires_in", 1800))),
            scopes=tokens.get("scope", ""),
            connected_by=request.user,
        )
        messages.success(request, f"Connected to {tenant.get('tenantName')}.")
        return redirect("crm:xero_status")


class XeroDisconnectView(FinanceRequiredMixin, View):
    """Revoke the grant at Xero (dropping every org authorised under it), then
    forget the tokens locally. Without the server-side revocation the org
    stays in Xero's connected apps and piles up on reconnect."""

    def post(self, request):
        from .. import xero as xero_helpers

        conn = XeroConnection.objects.first()
        if conn is not None:
            try:
                xero_helpers.revoke_connection(conn)
            except Exception:
                messages.warning(
                    request,
                    "Xero didn't confirm the revocation — the connection was "
                    "cleared locally, but check this app under Connected apps "
                    "in Xero if the organisation still shows there.",
                )
        XeroConnection.objects.all().delete()
        messages.success(request, "Xero disconnected.")
        return redirect("crm:xero_status")


class XeroSyncInvoicesView(FinanceRequiredMixin, View):
    """Pull the current status of every mirrored invoice back from Xero.
    Manual, fired from the nav's sync button — no background job needed at
    this volume."""

    def post(self, request):
        from .. import xero as xero_helpers

        if xero_helpers.get_active_connection() is None:
            messages.error(request, "Connect to Xero first.")
            return redirect("crm:xero_status")

        local = {inv.xero_invoice_id: inv for inv in XeroInvoice.objects.all()}
        if not local:
            messages.info(request, "No Xero invoices to sync yet.")
            return self._redirect_back(request)

        try:
            remote = xero_helpers.list_invoices(list(local))
        except xero_helpers.XeroError as exc:
            messages.error(request, str(exc))
            return self._redirect_back(request)

        updated = 0
        for data in remote:
            inv = local.get(data.get("InvoiceID"))
            if inv is None:
                continue
            fields = {
                "status": data.get("Status") or inv.status,
                "xero_invoice_number": data.get("InvoiceNumber") or inv.xero_invoice_number,
                "total": (
                    Decimal(str(data["Total"])) if data.get("Total") is not None else inv.total
                ),
            }
            changed = [name for name, value in fields.items() if getattr(inv, name) != value]
            if changed:
                for name in changed:
                    setattr(inv, name, fields[name])
                inv.save(update_fields=changed)
                updated += 1
        count = len(local)
        messages.success(
            request,
            f"Synced {count} Xero invoice{'s' if count != 1 else ''} — {updated} updated.",
        )
        return self._redirect_back(request)

    def _redirect_back(self, request):
        """Land back where the button was pressed — it lives in the nav, so
        that could be any staff page."""
        referer = request.META.get("HTTP_REFERER")
        if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(referer)
        return redirect("crm:xero_status")
