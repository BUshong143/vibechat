from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from .forms import ProfileForm, RegisterForm
from .models import User

def register(request):
    if request.user.is_authenticated:
        return redirect("/app/")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save())
        return redirect("/app/")
    return render(request, "register.html", {"form": form})

@login_required
def profile_edit(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(f"/u/{request.user.username}/")
    # `current` is re-read so the preview never shows a half-validated upload
    return render(request, "profile_edit.html", {"form": form, "current": User.objects.get(pk=request.user.pk)})
