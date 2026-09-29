# config/urls.py
from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include
from django.views.static import serve


def healthz(request):
    """Cheap liveness check for Railway (no database access)."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz/", healthz),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.social.urls")),
    path("", include("apps.chat.urls")),
    # Profile photos. Django's static() helper is dev-only, so serve MEDIA_ROOT explicitly; on Railway
    # MEDIA_ROOT sits on the attached Volume. Photos are small, public, 256x256 JPEGs.
    path("media/<path:path>", serve, {"document_root": settings.MEDIA_ROOT}),
]
