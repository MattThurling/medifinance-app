"""Move deposit, balloon and repayment_profile from Deal to Quote.

Each quote is now a self-contained financing structure. Existing deal-level
values are copied onto every quote of the deal before the deal columns are
dropped, so each quote's computed monthly payment is unchanged.
"""

from django.db import migrations, models


def copy_deal_terms_to_quotes(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Quote = apps.get_model("crm", "Quote")
    deals = Deal.objects.exclude(deposit=None, balloon=None, repayment_profile="")
    for deal in deals.iterator():
        Quote.objects.filter(deal=deal).update(
            deposit=deal.deposit,
            balloon=deal.balloon,
            repayment_profile=deal.repayment_profile,
        )


def copy_quote_terms_to_deals(apps, schema_editor):
    """Best-effort reverse: restore each deal's values from its selected quote,
    falling back to its first quote."""
    Deal = apps.get_model("crm", "Deal")
    for deal in Deal.objects.iterator():
        quote = None
        if deal.selected_quote_id:
            quote = deal.selected_quote
        else:
            quote = deal.quotes.order_by("pk").first()
        if quote is None:
            continue
        Deal.objects.filter(pk=deal.pk).update(
            deposit=quote.deposit,
            balloon=quote.balloon,
            repayment_profile=quote.repayment_profile,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0021_alter_note_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="deposit",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="balloon",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="repayment_profile",
            field=models.CharField(blank=True, help_text="Free-text repayment profile (e.g. monthly, quarterly, balloon). May be structured later.", max_length=100),
        ),
        migrations.RunPython(copy_deal_terms_to_quotes, copy_quote_terms_to_deals),
        migrations.RemoveField(
            model_name="deal",
            name="deposit",
        ),
        migrations.RemoveField(
            model_name="deal",
            name="balloon",
        ),
        migrations.RemoveField(
            model_name="deal",
            name="repayment_profile",
        ),
    ]
