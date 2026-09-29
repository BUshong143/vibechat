"""Group chat rules, kept in one place (like apps/social/services.py) so views and tests ask one module.

Who may do what: any member can add people, rename the group, or leave it. There is no owner,
because the database has no creator column. Members can only be added from the adder's own
friends, so nobody gets pulled into a group by a stranger.
"""
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.social import services as social
from .models import Conversation, ConversationMember

MAX_MEMBERS = 50
MAX_TITLE = 100
MIN_OTHERS = 2  # a group is you plus at least two people; one person is just a direct chat

User = get_user_model()


class GroupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def clean_title(raw):
    title = str(raw or "").strip()
    if len(title) > MAX_TITLE:
        raise GroupError(f"Group name can be at most {MAX_TITLE} characters")
    return title


def _friends_from(user, usernames):
    """Users named in `usernames`, all of whom must be the caller's friends. Duplicates and self are dropped."""
    if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
        raise GroupError("Pick people to add")
    wanted = {u for u in usernames if u and u != user.username}
    if not wanted:
        return []
    found = list(User.objects.filter(username__in=wanted, is_active=True))
    if len(found) != len(wanted):
        raise GroupError("Some of those people don't exist")
    friends = social.friend_ids(user)
    if any(u.pk not in friends for u in found):
        raise GroupError("You can only add your friends", 403)
    return found


def create_group(user, title, usernames):
    title = clean_title(title)
    people = _friends_from(user, usernames)
    if len(people) < MIN_OTHERS:
        raise GroupError(f"Pick at least {MIN_OTHERS} friends for a group")
    if len(people) + 1 > MAX_MEMBERS:
        raise GroupError(f"A group can have at most {MAX_MEMBERS} people")
    with transaction.atomic():
        conv = Conversation.objects.create(type=Conversation.GROUP, title=title)
        ConversationMember.objects.bulk_create(
            [ConversationMember(conversation=conv, user=u) for u in [user, *people]])
    return conv


def add_members(conv, user, usernames):
    """Add the caller's friends. People already in the group are skipped. Returns the users actually added."""
    people = _friends_from(user, usernames)
    if not people:
        raise GroupError("Pick people to add")
    with transaction.atomic():
        # lock the group row so two people adding at once can't push it over the limit
        Conversation.objects.select_for_update().get(pk=conv.pk)
        present = set(ConversationMember.objects.filter(conversation=conv).values_list("user_id", flat=True))
        new = [u for u in people if u.pk not in present]
        if len(present) + len(new) > MAX_MEMBERS:
            raise GroupError(f"A group can have at most {MAX_MEMBERS} people")
        ConversationMember.objects.bulk_create([ConversationMember(conversation=conv, user=u) for u in new])
    return new


def leave(conv, user):
    """Remove the caller. An empty group is deleted, since nobody can read or join it any more."""
    with transaction.atomic():
        ConversationMember.objects.filter(conversation=conv, user=user).delete()
        if not ConversationMember.objects.filter(conversation=conv).exists():
            conv.delete()
            return True
    return False
