"""Lender status → stage events, and the client approval email — shared by
the Proposal views (Asset Finance deals) and the Terms rows on commercial
deals (Quote views). Both record where a lender stands on a deal; the
bookkeeping around that is identical, so it lives here once."""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone

from .models import LenderStatus, Stage

# Map a lender status -> the Stage.Name to emit when a row is created with,
# or its status flips to, that status. WITHDRAWN deliberately absent:
# withdrawing one lender doesn't move the deal's stage — the row's own
# status records it.
LENDER_STATUS_TO_STAGE = {
    LenderStatus.SUBMITTED: Stage.Name.PROPOSAL_SUBMITTED,
    LenderStatus.APPROVED: Stage.Name.PROPOSAL_APPROVED,
    LenderStatus.DECLINED: Stage.Name.PROPOSAL_DECLINED,
}


def emit_status_stage(deal, status, lender, user) -> "Stage | None":
    """Append the stage event matching `status` (lender attached), or nothing
    for statuses that don't move the deal."""
    name = LENDER_STATUS_TO_STAGE.get(status)
    if not name:
        return None
    return Stage.objects.create(deal=deal, name=name, organisation=lender, set_by=user)


def notify_client_of_approval(
    request, *, deal, row, lender, reference, is_selected,
    not_approved_msg, not_selected_msg,
):
    """Email the deal's customer the good news about an approved lender and
    record it. `row` is the Proposal or Terms row (Quote): it needs `status`,
    `notified_at` and `save(update_fields=...)`.

    Only allowed while the row is Approved and is the deal's selected one,
    and only once — `notified_at` records (and blocks repeating) the send.
    Returns the redirect back to the deal."""
    if row.status != LenderStatus.APPROVED:
        messages.error(request, not_approved_msg)
        return redirect(deal.get_absolute_url())
    if not is_selected:
        messages.error(request, not_selected_msg)
        return redirect(deal.get_absolute_url())
    if row.notified_at is not None:
        messages.error(
            request,
            f"The client was already notified on {row.notified_at:%d %b %Y}.",
        )
        return redirect(deal.get_absolute_url())
    if deal.customer is None or not deal.customer.email:
        messages.error(
            request,
            "Can't notify the client: this deal's customer has no email address on file.",
        )
        return redirect(deal.get_absolute_url())

    finance = deal.finance_amount
    quote = deal.selected_quote
    monthly = quote.monthly_payment if quote else None

    from accounts.emails import send_proposal_approved_client_email
    send_proposal_approved_client_email(
        to_email=deal.customer.email,
        contact_first_name=deal.customer.first_name,
        lender_org_name=lender.name,
        proposal_number=reference,
        finance_amount_display=f"£{finance:,.2f}" if finance is not None else "",
        term_display=f"{quote.term} months" if quote and quote.term else "",
        monthly_payment_display=f"£{monthly:,.2f}" if monthly is not None else "",
    )

    row.notified_at = timezone.now()
    row.save(update_fields=["notified_at", "updated_at"])
    Stage.objects.create(
        deal=deal,
        name=Stage.Name.CLIENT_NOTIFIED,
        organisation=lender,
        set_by=request.user,
    )
    messages.success(request, f"Approval email sent to {deal.customer.email}.")
    return redirect(deal.get_absolute_url())
