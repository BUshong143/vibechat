from django.urls import path, re_path
from . import views

urlpatterns = [
    path("api/friends/", views.friends),
    re_path(r"^api/friends/(?P<username>[^/]+)/(?P<action>request|accept|decline|cancel|remove)/$", views.friend_action),
    path("u/<str:username>/", views.profile, name="profile"),
]
