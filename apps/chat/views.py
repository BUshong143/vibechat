import json
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import F, OuterRef, Q, Subquery
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from apps.social import services as social
from . import attachments as att
from . import calls, groups
from .models import Conversation, ConversationMember, Message, MessageAttachment
from .realtime import broadcast, mark_read, message_dict, messages_with_attachments, valid_id

User = get_user_model()

def _body(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}

def _other(conv, user):
    return next((m for m in conv.members.all() if m.pk != user.pk), None)

def _title(conv, user):
    if conv.type == Conversation.GROUP:
        if conv.title:
            return conv.title
        names = sorted(m.name for m in conv.members.all() if m.pk != user.pk)
        if not names:
            return "Group"
        return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    other = _other(conv, user)
    return other.name if other else "Empty chat"

def _preview_text(content, attachment_count=0):
    text = (content or "").strip()
    if text:
        return text[:60]
    if attachment_count:
        return "📎 Attachment" if attachment_count == 1 else f"📎 {attachment_count} attachments"
    return ""

def _conv_json(conv, user, friends):
    """`friends` is the set of the viewer's friend ids, so a list of chats needs no per-chat query."""
    if hasattr(conv, "last_text"):
        last = (conv.last_text or "").strip()
        # Attachment-only messages leave content empty; show a neutral preview.
        if not last and getattr(conv, "last_id", None):
            last = "📎 Attachment"
    else:
        m = conv.messages.prefetch_related("attachments").last()
        if m:
            last = _preview_text(m.content, m.attachments.count())
        else:
            last = ""
    is_group = conv.type == Conversation.GROUP
    other = None if is_group else _other(conv, user)
    title = _title(conv, user)
    if other:
        face = other.card()
    else:
        seed = conv.id if is_group else 0
        face = {"avatar": "", "initial": (title[:1] or "G").upper(),
                "color": f"hsl({seed * 47 % 360} 45% 45%)" if is_group else "hsl(215 16% 47%)"}
    # One-to-one chats stay readable after an unfriend, but you can only write to friends.
    can_send = is_group or (other is not None and other.pk in friends)
    data = {"id": conv.id, "type": conv.type, "title": title, "last": last[:60],
            "other": other.username if other else None, "face": face, "can_send": can_send}
    if is_group:
        data["custom_title"] = conv.title
        data["members"] = [m.card() for m in sorted(conv.members.all(), key=lambda m: m.name.lower())]
    return data

def _msg_json(m, user):
    return {**message_dict(m), "mine": m.sender_id == user.id}

@login_required
@ensure_csrf_cookie
def app(request):
    return render(request, "app.html")

@login_required
@require_http_methods(["GET", "POST"])
def conversations(request):
    user = request.user
    if request.method == "POST":
        body = _body(request)
        if body.get("type") == Conversation.GROUP:
            try:
                conv = groups.create_group(user, body.get("title"), body.get("usernames"))
            except groups.GroupError as e:
                return JsonResponse({"error": e.message}, status=e.status)
            _notify(conv, "created", user, added=conv.members.exclude(pk=user.pk).values_list("username", flat=True))
            return JsonResponse(_conv_json(conv, user, social.friend_ids(user)), status=201)
        other = User.objects.filter(username=body.get("username", "")).first()
        if not other or other.pk == user.pk:
            return JsonResponse({"error": "Invalid user"}, status=400)
        if not social.can_chat(user.pk, other.pk):
            return JsonResponse({"error": "You can only chat with friends"}, status=403)
        conv = (Conversation.objects.filter(type=Conversation.DIRECT, members=user)
                .filter(members=other).first())
        if not conv:
            conv = Conversation.objects.create(type=Conversation.DIRECT)
            ConversationMember.objects.bulk_create([
                ConversationMember(conversation=conv, user=user),
                ConversationMember(conversation=conv, user=other)])
        return JsonResponse(_conv_json(conv, user, social.friend_ids(user)), status=201)
    # A new group member never sees what was said before they joined, not even in the preview line.
    joined = ConversationMember.objects.filter(conversation=OuterRef("pk"), user=user).values("joined_at")[:1]
    latest = (Message.objects.filter(conversation=OuterRef("pk"), created_at__gte=OuterRef("joined"))
              .order_by("-id"))
    convs = (user.conversations
             .annotate(joined=Subquery(joined))
             .annotate(last_id=Subquery(latest.values("id")[:1]), last_text=Subquery(latest.values("content")[:1]))
             .order_by(F("last_id").desc(nulls_last=True), "-id")
             .prefetch_related("members"))
    friends = social.friend_ids(user)
    return JsonResponse({"conversations": [_conv_json(c, user, friends) for c in convs]})

def _collect_uploads(request):
    """Accept multipart field name `attachments` (multi) or `attachments[]`."""
    files = request.FILES.getlist("attachments") or request.FILES.getlist("attachments[]")
    return files


@login_required
@require_http_methods(["GET", "POST"])
def messages(request, cid):
    conv = get_object_or_404(Conversation, pk=cid, members=request.user)
    if request.method == "POST":
        if conv.type == Conversation.DIRECT:
            other = _other(conv, request.user)
            if not other or not social.can_chat(request.user.pk, other.pk):
                return JsonResponse({"error": "You can only message friends"}, status=403)

        # JSON body or multipart form fields
        if request.content_type and "multipart/form-data" in request.content_type:
            content = str(request.POST.get("content", "")).strip()
            uploads = _collect_uploads(request)
        else:
            body = _body(request)
            content = str(body.get("content", "")).strip()
            uploads = []

        if content and len(content) > 2000:
            return JsonResponse({"error": "Message must be at most 2000 characters"}, status=400)
        if not content and not uploads:
            return JsonResponse({"error": "Message must have text or attachments"}, status=400)
        if len(uploads) > att.MAX_ATTACHMENTS:
            return JsonResponse({"error": f"At most {att.MAX_ATTACHMENTS} attachments per message"}, status=400)

        classified = []
        for f in uploads:
            try:
                file_type, mime, name = att.classify(f)
            except att.AttachmentError as e:
                return JsonResponse({"error": e.message}, status=e.status)
            classified.append((f, file_type, mime, name))

        m = Message.objects.create(conversation=conv, sender=request.user, content=content)
        for f, file_type, mime, name in classified:
            thumb = att.make_thumbnail(f, file_type)
            try:
                f.seek(0)
            except Exception:
                pass
            row = MessageAttachment(
                message=m,
                original_name=name,
                file_type=file_type,
                mime_type=mime,
                file_size=f.size,
            )
            row.file.save(name, f, save=False)
            if thumb:
                row.thumbnail.save("thumb.jpg", thumb, save=False)
            row.save()

        # Reload with attachments for the payload
        m = Message.objects.select_related("sender").prefetch_related("attachments").get(pk=m.pk)
        payload = message_dict(m)
        try:
            broadcast(list(conv.members.values_list("id", flat=True)),
                      {"type": "message.new", "conversation": conv.id, "client_id": None, "message": payload})
        except Exception:
            pass
        return JsonResponse({**payload, "mine": True}, status=201)

    rows = list(ConversationMember.objects.filter(conversation=conv)
                .values_list("user__username", "user_id", "last_read_id", "joined_at"))
    mine = next((r for r in rows if r[1] == request.user.id), None)
    if mine is None:  # removed a moment ago
        raise Http404("Not a member")
    qs = messages_with_attachments(conv.messages.filter(created_at__gte=mine[3]))  # nothing from before you joined
    after = request.GET.get("after", "")
    if after.isdigit():
        qs = qs.filter(id__gt=int(after))
    reads = {name: last for name, uid, last, _ in rows if uid != request.user.id}
    return JsonResponse({
        "messages": [_msg_json(m, request.user) for m in qs[:200]],
        "my_read": mine[2],
        "other_read": max(reads.values(), default=0),
        "reads": reads,  # username -> last message id that person has seen (drives "Seen by")
    })

@login_required
@require_POST
def read(request, cid):
    conv = get_object_or_404(Conversation, pk=cid, members=request.user)
    message_id = _body(request).get("message_id")
    if not valid_id(message_id):
        return JsonResponse({"error": "Invalid message"}, status=400)
    upto = mark_read(request.user, conv.id, message_id)
    if upto:
        try:
            others = [i for i in conv.members.values_list("id", flat=True) if i != request.user.id]
            broadcast(others, {"type": "read", "conversation": conv.id, "user": request.user.username, "message_id": upto})
        except Exception:
            pass
    return JsonResponse({"read": upto})

def _notify(conv, event, actor, extra_ids=(), added=()):
    """Tell every member (plus anyone who just left) that the group changed, so their tabs refresh."""
    try:
        ids = set(conv.members.values_list("id", flat=True)) | set(extra_ids)
        broadcast(ids, {"type": "conversation.changed", "conversation": conv.id, "event": event,
                        "user": actor.username, "name": actor.name, "title": conv.title, "added": list(added)})
    except Exception:
        pass

def _group_or_404(request, cid):
    conv = get_object_or_404(Conversation, pk=cid, members=request.user)
    if conv.type != Conversation.GROUP:
        raise Http404("Not a group")
    return conv

@login_required
@require_POST
def group_members(request, cid):
    conv = _group_or_404(request, cid)
    try:
        added = groups.add_members(conv, request.user, _body(request).get("usernames"))
    except groups.GroupError as e:
        return JsonResponse({"error": e.message}, status=e.status)
    if added:
        _notify(conv, "added", request.user, added=[u.username for u in added])
    return JsonResponse({**_conv_json(conv, request.user, social.friend_ids(request.user)),
                         "added": [u.username for u in added]})

@login_required
@require_POST
def group_title(request, cid):
    conv = _group_or_404(request, cid)
    try:
        conv.title = groups.clean_title(_body(request).get("title"))
    except groups.GroupError as e:
        return JsonResponse({"error": e.message}, status=e.status)
    conv.save(update_fields=["title"])
    _notify(conv, "renamed", request.user)
    return JsonResponse(_conv_json(conv, request.user, social.friend_ids(request.user)))

@login_required
@require_POST
def group_leave(request, cid):
    conv = _group_or_404(request, cid)
    gone = groups.leave(conv, request.user)
    if not gone:
        _notify(conv, "left", request.user, extra_ids=[request.user.id])
    else:
        try:
            broadcast([request.user.id], {"type": "conversation.changed", "conversation": cid, "event": "left",
                                          "user": request.user.username, "name": request.user.name, "title": ""})
        except Exception:
            pass
    return JsonResponse({"left": True})

@login_required
@require_GET
def user_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"users": []})
    users = list(User.objects.filter(is_active=True).filter(Q(username__icontains=q) | Q(display_name__icontains=q))
                 .exclude(pk=request.user.pk).order_by("username")[:10])
    status = social.relationships(request.user, users)
    return JsonResponse({"users": [{**u.card(), "status": status.get(u.pk, social.NONE)} for u in users]})

@login_required
@require_GET
def call_config(request):
    """ICE servers for a call. Fetched per call so TURN credentials can be short-lived."""
    return JsonResponse({"iceServers": calls.ice_servers(request.user)})
