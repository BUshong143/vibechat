from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Max

from apps.social import services as social
from .models import ConversationMember, Message


def message_dict(m):
    return {"id": m.id, "sender": m.sender.username, "content": m.content, "created_at": m.created_at.isoformat()}


def broadcast(member_ids, payload):
    layer = get_channel_layer()
    if layer is None:
        return
    for uid in member_ids:
        async_to_sync(layer.group_send)(f"user_{uid}", {"type": "relay", "payload": payload})


def mark_read(user, cid, message_id):
    upto = Message.objects.filter(conversation_id=cid, id__lte=message_id).aggregate(m=Max("id"))["m"]
    if not upto:
        return 0
    changed = ConversationMember.objects.filter(
        conversation_id=cid, user=user, last_read_id__lt=upto
    ).update(last_read_id=upto)
    return upto if changed else 0


def contacts_of(user):
    conv_ids = ConversationMember.objects.filter(user=user).values("conversation_id")
    rows = (ConversationMember.objects.filter(conversation_id__in=conv_ids)
            .exclude(user=user).values_list("user_id", "user__username").distinct())
    contacts = dict(rows)
    contacts.update(social.friend_names(user))  # friends see each other's presence before any chat exists
    return contacts


def valid_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
