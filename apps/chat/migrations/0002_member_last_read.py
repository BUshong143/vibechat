from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversationmember',
            name='last_read_id',
            field=models.BigIntegerField(default=0),
        ),
    ]
