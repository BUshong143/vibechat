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
    content = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
