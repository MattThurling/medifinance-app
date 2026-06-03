"""Contact ↔ Organisation becomes many-to-many, and Deal gains its own
`organisation` FK separate from `customer`.

The data migration copies each contact's single `organisation` into the new
`organisations` M2M, and seeds each deal's `organisation` from
`customer.organisation` (the previous derivation), preserving the old behaviour
of the UI without losing any data.
"""

import django.db.models.deletion
from django.db import migrations, models


def copy_org_data(apps, schema_editor):
    """Contact.organisation → Contact.organisations.add(), and
    Deal.organisation = Contact.organisation (the old derivation)."""
    Contact = apps.get_model("crm", "Contact")
    Deal = apps.get_model("crm", "Deal")
    db_alias = schema_editor.connection.alias

    # Through table for the new M2M:
    Through = Contact.organisations.through

    through_rows = [
        Through(contact_id=c_id, organisation_id=org_id)
        for c_id, org_id in Contact.objects.using(db_alias)
        .exclude(organisation__isnull=True)
        .values_list("id", "organisation_id")
    ]
    Through.objects.using(db_alias).bulk_create(
        through_rows, batch_size=500, ignore_conflicts=True
    )

    # Seed Deal.organisation from each deal's customer's (old) organisation.
    contact_to_org = dict(
        Contact.objects.using(db_alias)
        .exclude(organisation__isnull=True)
        .values_list("id", "organisation_id")
    )
    for d_id, cust_id in Deal.objects.using(db_alias).values_list("id", "customer_id"):
        org_id = contact_to_org.get(cust_id)
        if org_id is not None:
            Deal.objects.using(db_alias).filter(pk=d_id).update(organisation_id=org_id)


def reverse_copy_org_data(apps, schema_editor):
    """Reverse: pick one organisation per contact from the M2M (Django will
    restore the column via reverse RemoveField). Lossy by definition — a contact
    with multiple orgs collapses to one."""
    Contact = apps.get_model("crm", "Contact")
    db_alias = schema_editor.connection.alias
    for c in Contact.objects.using(db_alias).iterator():
        first = c.organisations.first()
        if first is not None:
            c.organisation_id = first.id
            c.save(update_fields=["organisation"])


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_remove_deal_equipment_supplier"),
    ]

    operations = [
        # 1. New M2M (creates through table) and new Deal FK column.
        migrations.AddField(
            model_name="contact",
            name="organisations",
            field=models.ManyToManyField(
                blank=True,
                help_text="A contact can belong to more than one organisation.",
                related_name="contacts",
                to="crm.organisation",
            ),
        ),
        migrations.AddField(
            model_name="deal",
            name="organisation",
            field=models.ForeignKey(
                blank=True,
                help_text="The organisation this deal is for. May be different from any of the customer's organisations; can be set later.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deals",
                to="crm.organisation",
            ),
        ),
        # 2. Copy data while the old Contact.organisation FK is still present.
        migrations.RunPython(copy_org_data, reverse_copy_org_data),
        # 3. Drop the old single-FK column.
        migrations.RemoveField(
            model_name="contact",
            name="organisation",
        ),
    ]
