from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_user_organisation"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="totp_secret",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="user",
            name="totp_confirmed_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="totp_last_used_step",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
    ]
