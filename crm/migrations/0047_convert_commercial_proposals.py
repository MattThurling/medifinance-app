"""Fold Proposals on commercial-finance deals into Terms rows (Quote).

Commercial deals (every type except asset_finance) now show one "Terms" box
instead of separate Quotes and Proposals. Each Proposal on such a deal
becomes a Quote carrying the lender, status, reference and notified date,
with no term yet (the lender's terms are entered when they come back).
Existing commercial quotes get a status: Approved if they already carry a
rate and term (the lender clearly came back), else Submitted.

Kept apart from 0046 so the data step never shares a transaction with the
schema change (Postgres "pending trigger events") and can be inspected on
its own.
"""

from django.db import migrations


ASSET_FINANCE = "asset_finance"


def _commercial(deal_model):
    return deal_model.objects.exclude(type__isnull=True).exclude(type=ASSET_FINANCE)


def forward(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Proposal = apps.get_model("crm", "Proposal")
    Quote = apps.get_model("crm", "Quote")

    commercial_ids = set(_commercial(Deal).values_list("pk", flat=True))

    # Existing commercial quotes: infer a status from whether terms exist.
    for quote in Quote.objects.filter(deal_id__in=commercial_ids, status=""):
        has_terms = quote.interest_rate_percent is not None and quote.term is not None
        quote.status = "approved" if has_terms else "submitted"
        quote.save(update_fields=["status"])

    proposals = (
        Proposal.objects.filter(deal_id__in=commercial_ids)
        .select_related("deal")
        .order_by("created_at", "pk")
    )
    for proposal in proposals:
        quote = Quote.objects.create(
            deal_id=proposal.deal_id,
            lender_id=proposal.lender_id,
            status=proposal.status,
            lender_reference=proposal.proposal_number,
            notified_at=proposal.notified_at,
            term=None,
        )
        # auto_now_add/auto_now override values passed to create(); keep the
        # original timestamps so last-activity ordering is preserved.
        Quote.objects.filter(pk=quote.pk).update(
            created_at=proposal.created_at, updated_at=proposal.updated_at,
        )
        deal = proposal.deal
        if deal.selected_proposal_id == proposal.pk:
            updates = {"selected_proposal": None}
            if deal.selected_quote_id is None:
                updates["selected_quote"] = quote
            Deal.objects.filter(pk=deal.pk).update(**updates)
        proposal.delete()


def backward(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Proposal = apps.get_model("crm", "Proposal")
    Quote = apps.get_model("crm", "Quote")

    commercial_ids = set(_commercial(Deal).values_list("pk", flat=True))

    # Only term-less rows go back to being Proposals: a row with terms stays
    # a plain quote (0046's reverse drops its status/reference columns), so a
    # reverse-then-forward round trip doesn't duplicate it.
    rows = (
        Quote.objects.filter(deal_id__in=commercial_ids, rate__isnull=True, term__isnull=True)
        .exclude(status="")
        .exclude(lender__isnull=True)
        .select_related("deal")
        .order_by("created_at", "pk")
    )
    for quote in rows:
        proposal = Proposal.objects.create(
            deal_id=quote.deal_id,
            lender_id=quote.lender_id,
            proposal_number=quote.lender_reference,
            status=quote.status,
            notified_at=quote.notified_at,
        )
        Proposal.objects.filter(pk=proposal.pk).update(
            created_at=quote.created_at, updated_at=quote.updated_at,
        )
        if quote.deal.selected_quote_id == quote.pk:
            Deal.objects.filter(pk=quote.deal_id).update(selected_proposal=proposal)

    # Term-less rows cannot survive 0046's NOT NULL restore.
    Quote.objects.filter(deal_id__in=commercial_ids, term__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0046_quote_terms_fields"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
