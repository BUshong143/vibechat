from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.chat.realtime import broadcast
from . import services

User = get_user_model()

ACTIONS = {
    "request": lambda me, other: services.send_request(me, other),
    "accept": services.accept,
    "decline": services.decline,
    "cancel": services.cancel,
    "remove": services.remove,
}


@login_required
@require_GET
def friends(request):
    friends, incoming, outgoing = services.friend_lists(request.user)
    return JsonResponse({"friends": [u.card() for u in friends],
                         "incoming": [u.card() for u in incoming],
                         "outgoing": [u.card() for u in outgoing]})


@login_required
@require_POST
def friend_action(request, username, action):
    me = request.user
    other = User.objects.filter(username=username, is_active=True).first()
    if not other:
        return JsonResponse({"error": "User not found"}, status=404)
    if other.pk == me.pk:
        return JsonResponse({"error": "You can't do that to yourself"}, status=400)
    before = services.relationship(me, other)
    if not ACTIONS[action](me, other):
        return JsonResponse({"error": "That request no longer exists"}, status=409)
    after = services.relationship(me, other)
    if after != before:  # tell both people's open tabs to refresh
        if after == services.FRIENDS:
            event = "accepted"
        elif after == services.OUTGOING:
            event = "request"  # the other person now has a new incoming request
        else:
            event = "changed"
        try:
            broadcast([me.pk, other.pk], {"type": "friends.changed", "event": event, "user": me.username, "name": me.name})
        except Exception:
            pass
    return JsonResponse({"status": after})


@login_required
@require_GET
def profile(request, username):
    person = get_object_or_404(User, username=username, is_active=True)
    rel = "self" if person.pk == request.user.pk else services.relationship(request.user, person)
    return render(request, "profile.html", {"person": person, "rel": rel})
