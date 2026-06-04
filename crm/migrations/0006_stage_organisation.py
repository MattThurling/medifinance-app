# Stage gains an organisation FK + new choice values. The data step backfills
# existing rows from each stage's deal.organisation so old timeline entries
# also display an org under the stage name.

import django.db.models.deletion
from django.db import migrations, models


def backfill_stage_organisation(apps, schema_editor):
    from django.db.models import OuterRef, Subquery

    Stage = apps.get_model("crm", "Stage")
    Deal = apps.get_model("crm", "Deal")
    db_alias = schema_editor.connection.alias

    deal_org = (
        Deal.objects.using(db_alias)
        .filter(pk=OuterRef("deal_id"))
        .values("organisation_id")[:1]
    )
    Stage.objects.using(db_alias).filter(organisation__isnull=True).update(
        organisation_id=Subquery(deal_org)
    )


def _noop_reverse(apps, schema_editor):
    """Reverse is a no-op — the column is dropped by reversing AddField anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0005_drop_optional_help_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='stage',
            name='organisation',
            field=models.ForeignKey(blank=True, help_text='Which organisation this stage is about — the client, the lender (for proposal stages), or the supplier (for invoice stages).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='crm.organisation'),
        ),
        migrations.AlterField(
            model_name='stage',
            name='name',
            field=models.CharField(choices=[('application', 'Application'), ('info_received', 'Info Received'), ('proposal_submitted', 'Proposal Submitted'), ('proposal_approved', 'Proposal Approved'), ('proposal_declined', 'Proposal Declined'), ('proposal_withdrawn', 'Proposal Withdrawn'), ('invoice_requested', 'Invoice Requested'), ('invoice_received', 'Invoice Received')], max_length=32),
        ),
        migrations.RunPython(backfill_stage_organisation, _noop_reverse),
    ]
