import uuid

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    DIRECT, GROUP = "direct", "group"
    type = models.CharField(max_length=10, choices=[(DIRECT, "Direct"), (GROUP, "Group")], default=DIRECT)
    title = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through="ConversationMember", related_name="conversations")


class ConversationMember(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read_id = models.BigIntegerField(default=0)

    class Meta:
        unique_together = ("conversation", "user")


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages")
    content = models.TextField(max_length=2000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


def attachment_path(instance, filename):
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "bin")[:12].lower()
    return f"chat_attachments/{instance.message.conversation_id}/{uuid.uuid4().hex}.{ext}"


def thumbnail_path(instance, filename):
    return f"chat_attachments/{instance.message.conversation_id}/thumbs/{uuid.uuid4().hex}.jpg"


class MessageAttachment(models.Model):
    """Reusable file attachment linked to a chat message.

    Storage layout is conversation-scoped so the same file can later be linked into
    a Group Workspace without re-uploading (share the FileField path / storage key).
    """
    IMAGE, VIDEO, DOCUMENT, ARCHIVE = "image", "video", "document", "archive"
    TYPE_CHOICES = [
        (IMAGE, "Image"),
        (VIDEO, "Video"),
        (DOCUMENT, "Document"),
        (ARCHIVE, "Archive"),
    ]

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path)
    original_name = models.CharField(max_length=255)
    file_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    mime_type = models.CharField(max_length=128)
    file_size = models.PositiveBigIntegerField()
    thumbnail = models.ImageField(upload_to=thumbnail_path, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def url(self):
        return self.file.url if self.file else ""

    def thumb_url(self):
        return self.thumbnail.url if self.thumbnail else ""

    def as_dict(self):
        return {
            "id": self.id,
            "url": self.url(),
            "thumbnail": self.thumb_url() or None,
            "original_name": self.original_name,
            "file_type": self.file_type,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
        }
