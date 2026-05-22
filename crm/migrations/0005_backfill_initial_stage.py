"""Give every existing deal an initial 'Application' stage event.

Bulk-creates rather than going through `Deal.save()` so the post_save signal
doesn't fire again. Uses each deal's `created_at` as the stage timestamp.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Stage = apps.get_model("crm", "Stage")

    deals_without_stage = Deal.objects.filter(stage_events__isnull=True)
    Stage.objects.bulk_create(
        [
            Stage(
                deal=deal,
                name="application",
                occurred_at=deal.created_at,
                set_by=deal.owner,
            )
            for deal in deals_without_stage
        ]
    )


def reverse(apps, schema_editor):
    # Reversing the schema migration drops the Stage table anyway; nothing to do here.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0004_stage"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=reverse),
    ]
