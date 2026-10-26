from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from accounts.permissions import StaffRequiredMixin

from ..access import get_visible_deal_or_404, visible_contacts


class _PortalLinkMixin(StaffRequiredMixin):
    """Shared logic for minting a portal magic link from a Deal.

    The link is for the deal's customer unless the POST names a ``contact``
    (one of the deal's co-applicants — each applicant gets their own login).
    Auto-creates (or reuses) that person's User account, then issues a
    single-use MagicLink. Adds error/warning flash messages as needed.
    Returns ``(link, full_url)`` on success, or ``(None, None)`` on failure.
    """

    def resolve_contact(self, request, deal):
        """The contact the link is for: the POSTed ``contact`` pk (404 if the
        staff user can't see that contact) or the deal's customer."""
        pk = request.POST.get("contact")
        if not pk:
            return deal.customer
        return get_object_or_404(visible_contacts(request.user), pk=pk)

    def issue_link(self, request, deal, contact):
        from ..portal_links import (
            ContactNotOnDealError,
            NoCustomerEmailError,
            issue_portal_link_for_deal,
        )
        try:
            link, dup_warning = issue_portal_link_for_deal(
                deal, contact=contact, created_by=request.user,
            )
        except (NoCustomerEmailError, ContactNotOnDealError) as exc:
            messages.error(request, str(exc))
            return None, None
        if dup_warning:
            messages.warning(request, dup_warning)
        full_url = request.build_absolute_uri(reverse("consume_magic_link", args=[link.token]))
        return link, full_url


class IssuePortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and show its URL to staff for manual sending."""

    def post(self, request, pk):
        deal = get_visible_deal_or_404(request.user, pk, "customer")
        contact = self.resolve_contact(request, deal)
        link, full_url = self.issue_link(request, deal, contact)
        if link is None:
            return redirect(deal.get_absolute_url())
        messages.success(
            request,
            f"Portal link generated for {link.user.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}. URL: {full_url}",
        )
        return redirect(deal.get_absolute_url())


class EmailPortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and email it to the applicant via the configured backend."""

    def post(self, request, pk):
        deal = get_visible_deal_or_404(request.user, pk, "customer", "owner")
        contact = self.resolve_contact(request, deal)
        link, full_url = self.issue_link(request, deal, contact)
        if link is None:
            return redirect(deal.get_absolute_url())

        from accounts.emails import send_magic_link_email
        send_magic_link_email(
            to_email=link.user.email,
            link_url=full_url,
            deal_name=deal.name,
            owner_name=deal.owner.full_name if deal.owner else "The team",
            expires_at=link.expires_at,
        )
        messages.success(
            request,
            f"Portal link emailed to {link.user.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}.",
        )
        return redirect(deal.get_absolute_url())
