from django.urls import path
from . import views

urlpatterns = [
    path("app/", views.app, name="app"),
    path("api/conversations/", views.conversations),
    path("api/conversations/<int:cid>/messages/", views.messages),
    path("api/conversations/<int:cid>/read/", views.read),
    path("api/conversations/<int:cid>/members/", views.group_members),
    path("api/conversations/<int:cid>/title/", views.group_title),
    path("api/conversations/<int:cid>/leave/", views.group_leave),
    path("api/calls/config/", views.call_config),
    path("api/users/search/", views.user_search),
]
