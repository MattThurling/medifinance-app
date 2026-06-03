"""Replace Deal.funded_amount with the Participation model.

A deal's funded amount is now the sum of its Participations. Existing
`funded_amount` values are migrated into a single Participation per deal
(amount = funded_amount, organisation = equipment_supplier when set), preserving
the old data without losing the implicit supplier-amount link.
"""

from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


def funded_amount_to_participations(apps, schema_editor):
    """For each deal with a funded_amount, create a single Participation that
    captures it (with the equipment_supplier as the org, when present)."""
    Deal = apps.get_model("crm", "Deal")
    Participation = apps.get_model("crm", "Participation")
    db_alias = schema_editor.connection.alias

    to_create = []
    for d in Deal.objects.using(db_alias).exclude(funded_amount__isnull=True).iterator():
        to_create.append(
            Participation(
                deal=d,
                organisation_id=d.equipment_supplier_id,
                amount=d.funded_amount,
            )
        )
    Participation.objects.using(db_alias).bulk_create(to_create, batch_size=500)


def participations_back_to_funded_amount(apps, schema_editor):
    """Reverse: sum each deal's participations back into funded_amount. The
    column is restored by the reversed RemoveField, so it exists at this point."""
    Deal = apps.get_model("crm", "Deal")
    db_alias = schema_editor.connection.alias

    for d in Deal.objects.using(db_alias).all().iterator():
        total = sum((p.amount for p in d.participations.all()), Decimal("0"))
        d.funded_amount = total if total else None
        d.save(update_fields=["funded_amount"])


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0009_alter_contact_organisation"),
    ]

    operations = [
        # 1. Create the Participation table.
        migrations.CreateModel(
            name="Participation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "deal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="participations",
                        to="crm.deal",
                    ),
                ),
                (
                    "organisation",
                    models.ForeignKey(
                        blank=True,
                        help_text="The supplier this amount goes to. Optional — may be added later.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="participations",
                        to="crm.organisation",
                    ),
                ),
            ],
            options={"ordering": ["pk"]},
        ),
        # 2. Copy each Deal's funded_amount into a Participation row.
        migrations.RunPython(
            funded_amount_to_participations,
            participations_back_to_funded_amount,
        ),
        # 3. Drop the now-redundant funded_amount column from Deal.
        migrations.RemoveField(
            model_name="deal",
            name="funded_amount",
        ),
    ]
