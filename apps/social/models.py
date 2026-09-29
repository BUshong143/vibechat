from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Greatest, Least


class Friendship(models.Model):
    """One row per pair of people, whichever direction the request went.

    pending  -> from_user asked, to_user has not answered yet
    accepted -> they are friends
    Declining or cancelling deletes the row, so the pair is back to strangers.
    """
    PENDING, ACCEPTED = "pending", "accepted"
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="requests_sent")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="requests_received")
    status = models.CharField(max_length=10, choices=[(PENDING, "Pending"), (ACCEPTED, "Accepted")], default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(from_user=F("to_user")), name="friendship_not_self"),
            # A->B and B->A count as the same pair, so two people can never hold two rows.
            models.UniqueConstraint(Least("from_user", "to_user"), Greatest("from_user", "to_user"),
                                    name="friendship_unique_pair"),
        ]
        indexes = [
            models.Index(fields=["to_user", "status"]),
            models.Index(fields=["from_user", "status"]),
        ]

    def __str__(self):
        return f"{self.from_user} -> {self.to_user} ({self.status})"
