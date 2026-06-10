"""Add a required `organisation` FK to ApiKey and update the `name` field's
help text. Safe because the ApiKey table is empty in every environment — the
feature shipped on the previous commit and no real keys have been issued.

Written by hand instead of via makemigrations because adding a NOT NULL FK
needs a default in the autogenerator, and we don't want to fabricate one for
a table we know is empty."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_apikey'),
        ('crm', '0013_remove_quote_monthly_payment'),
    ]

    operations = [
        migrations.AddField(
            model_name='apikey',
            name='organisation',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='api_keys',
                to='crm.organisation',
                help_text='The partner organisation this key belongs to.',
            ),
        ),
        migrations.AlterField(
            model_name='apikey',
            name='name',
            field=models.CharField(
                help_text=(
                    "Label for this specific key, e.g. 'Production', 'Sandbox'. "
                    "Distinguishes keys within the same organisation."
                ),
                max_length=120,
            ),
        ),
    ]
