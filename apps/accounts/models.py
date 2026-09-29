import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


def avatar_path(instance, filename):
    # Random name per upload: old URLs never serve a stale picture from the browser cache.
    return f"avatars/{instance.pk}-{uuid.uuid4().hex[:10]}.jpg"


class User(AbstractUser):
    """Custom user model (set up first, as the blueprint advises)."""
    display_name = models.CharField(max_length=60, blank=True)
    avatar = models.ImageField(upload_to=avatar_path, blank=True)

    def __str__(self):
        return self.username

    @property
    def name(self):
        """What other people see: display name, falling back to the username."""
        return self.display_name or self.username

    @property
    def avatar_url(self):
        return self.avatar.url if self.avatar else ""

    @property
    def initial(self):
        return (self.name[:1] or "?").upper()

    @property
    def color(self):
        """Stable colour for the initials avatar shown when there is no photo."""
        return f"hsl({sum(map(ord, self.username)) * 37 % 360} 55% 42%)"

    def card(self):
        """Public, JSON-safe summary used by every API that lists people."""
        return {"username": self.username, "name": self.name, "avatar": self.avatar_url,
                "initial": self.initial, "color": self.color}
