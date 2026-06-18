from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0016_deal_balloon_deal_deposit'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='selected_proposal',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='+',
                to='crm.proposal',
            ),
        ),
    ]
