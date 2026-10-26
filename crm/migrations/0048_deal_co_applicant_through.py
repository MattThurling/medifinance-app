"""Give ``Deal.co_applicants`` an explicit through model, ``DealCoApplicant``.

The plain M2M's auto table (``crm_deal_co_applicants``: id, deal_id,
contact_id) is kept as-is — the model swap is a state-only operation — and
three columns are added so the row can say who attached the person and
when. ``added_by_customer`` marks applicants the primary customer added from
the portal, which staff see as "unverified".

The auto table already has a unique constraint on (deal_id, contact_id);
declaring it in state too keeps SQLite's table rebuild (triggered by the
first AddField) from dropping it, and Django introspects constraint names
when it later needs to drop one, so the auto-generated name is fine.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0047_convert_commercial_proposals"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="DealCoApplicant",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("deal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="co_applicant_links", to="crm.deal")),
                        ("contact", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="co_applicant_links", to="crm.contact")),
                    ],
                    options={"db_table": "crm_deal_co_applicants", "unique_together": {("deal", "contact")}},
                ),
                migrations.AlterField(
                    model_name="deal",
                    name="co_applicants",
                    field=models.ManyToManyField(blank=True, related_name="co_applicant_deals", through="crm.DealCoApplicant", to="crm.contact"),
                ),
            ],
            database_operations=[],
        ),
        migrations.AddField(
            model_name="dealcoapplicant",
            name="added_by_customer",
            field=models.BooleanField(default=False, help_text="True when the deal's primary customer added this person from the portal."),
        ),
        migrations.AddField(
            model_name="dealcoapplicant",
            name="added_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="dealcoapplicant",
            name="added_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
