from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from django.urls import path
from . import views
from .forms import LoginForm

urlpatterns = [
    path("", lambda r: redirect("/app/")),
    path("login/", auth_views.LoginView.as_view(template_name="login.html", authentication_form=LoginForm, redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("profile/edit/", views.profile_edit, name="profile_edit"),
]
