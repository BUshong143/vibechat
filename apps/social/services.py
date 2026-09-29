"""All friendship rules live here, so views, websockets and other apps ask one place.

Blocking (a later step) plugs in at `can_chat` and `relationship`: nothing else has to change.
"""
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Friendship

NONE, FRIENDS, OUTGOING, INCOMING = "none", "friends", "outgoing", "incoming"


def _pair(a, b):
    return Friendship.objects.filter(Q(from_user=a, to_user=b) | Q(from_user=b, to_user=a))


def _status_for(row, me_id):
    if row.status == Friendship.ACCEPTED:
        return FRIENDS
    return OUTGOING if row.from_user_id == me_id else INCOMING


def relationship(me, other):
    """How `me` sees `other`: none / friends / outgoing (I asked) / incoming (they asked)."""
    row = _pair(me, other).first()
    return _status_for(row, me.pk) if row else NONE


def relationships(me, users):
    """Same as `relationship`, for many people in one query. Returns {user_id: status}."""
    ids = [u.pk for u in users]
    rows = Friendship.objects.filter(Q(from_user=me, to_user_id__in=ids) | Q(to_user=me, from_user_id__in=ids))
    return {(r.to_user_id if r.from_user_id == me.pk else r.from_user_id): _status_for(r, me.pk) for r in rows}


def are_friends(a_id, b_id):
    return Friendship.objects.filter(status=Friendship.ACCEPTED).filter(
        Q(from_user_id=a_id, to_user_id=b_id) | Q(from_user_id=b_id, to_user_id=a_id)).exists()


def can_chat(a_id, b_id):
    """The single gate for starting or continuing a one-to-one chat."""
    return are_friends(a_id, b_id)


def friend_ids(user):
    return set(friend_names(user))


def friend_names(user):
    """{friend_id: username} for everyone `user` is friends with."""
    rows = (Friendship.objects.filter(status=Friendship.ACCEPTED).filter(Q(from_user=user) | Q(to_user=user))
            .values_list("from_user_id", "from_user__username", "to_user_id", "to_user__username"))
    return {(t if f == user.pk else f): (tn if f == user.pk else fn) for f, fn, t, tn in rows}


def friend_lists(user):
    """(friends, incoming, outgoing) as lists of User objects, in one query."""
    rows = (Friendship.objects.filter(Q(from_user=user) | Q(to_user=user))
            .select_related("from_user", "to_user").order_by("-created_at"))
    friends, incoming, outgoing = [], [], []
    for r in rows:
        other = r.to_user if r.from_user_id == user.pk else r.from_user
        status = _status_for(r, user.pk)
        {FRIENDS: friends, INCOMING: incoming, OUTGOING: outgoing}[status].append(other)
    friends.sort(key=lambda u: u.name.lower())
    return friends, incoming, outgoing


def accept(me, other):
    """Accept a request `other` sent to `me`. True if there was one to accept."""
    return bool(Friendship.objects.filter(from_user=other, to_user=me, status=Friendship.PENDING)
                .update(status=Friendship.ACCEPTED, responded_at=timezone.now()))


def decline(me, other):
    return bool(Friendship.objects.filter(from_user=other, to_user=me, status=Friendship.PENDING).delete()[0])


def cancel(me, other):
    return bool(Friendship.objects.filter(from_user=me, to_user=other, status=Friendship.PENDING).delete()[0])


def remove(me, other):
    return bool(_pair(me, other).filter(status=Friendship.ACCEPTED).delete()[0])


def send_request(me, other):
    """Ask `other` to be friends. Safe to call twice.

    If `other` already asked `me`, that is mutual interest, so it becomes a friendship.
    """
    rel = relationship(me, other)
    if rel in (FRIENDS, OUTGOING):
        return rel
    if rel == INCOMING:
        accept(me, other)
        return relationship(me, other)
    try:
        with transaction.atomic():
            Friendship.objects.create(from_user=me, to_user=other)
    except IntegrityError:  # both people clicked "Add" at the same moment
        if relationship(me, other) == INCOMING:
            accept(me, other)
        return relationship(me, other)
    return OUTGOING
