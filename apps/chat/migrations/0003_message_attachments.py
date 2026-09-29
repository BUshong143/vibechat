import apps.chat.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0002_member_last_read"),
    ]

    operations = [
        migrations.AlterField(
            model_name="message",
            name="content",
            field=models.TextField(blank=True, default="", max_length=2000),
        ),
        migrations.CreateModel(
            name="MessageAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=apps.chat.models.attachment_path)),
                ("original_name", models.CharField(max_length=255)),
                ("file_type", models.CharField(choices=[("image", "Image"), ("video", "Video"), ("document", "Document"), ("archive", "Archive")], max_length=16)),
                ("mime_type", models.CharField(max_length=128)),
                ("file_size", models.PositiveBigIntegerField()),
                ("thumbnail", models.ImageField(blank=True, upload_to=apps.chat.models.thumbnail_path)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="chat.message")),
            ],
            options={
                "ordering": ["id"],
            },
        ),
    ]
