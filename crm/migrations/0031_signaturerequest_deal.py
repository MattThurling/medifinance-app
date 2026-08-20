# Re-key SignatureRequest from Document to Deal. Existing rows are dev-stage
# throwaways — delete them so the non-null deal FK can be added cleanly.

import crm.models
import django.db.models.deletion
from django.db import migrations, models


def delete_signature_requests(apps, schema_editor):
    apps.get_model("crm", "SignatureRequest").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0030_signaturerequest'),
    ]

    operations = [
        migrations.RunPython(delete_signature_requests, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='signaturerequest',
            name='document',
        ),
        migrations.AddField(
            model_name='signaturerequest',
            name='deal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='signature_requests', to='crm.deal'),
        ),
        migrations.AddField(
            model_name='signaturerequest',
            name='signed_file',
            field=models.FileField(blank=True, null=True, upload_to=crm.models.signature_signed_upload_path),
        ),
    ]
