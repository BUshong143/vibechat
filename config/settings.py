# config/settings.py
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, unquote

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _csv(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# Railway sets RAILWAY_ENVIRONMENT / RAILWAY_PROJECT_ID on every deployed service.
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))

# Safe by default on Railway (DEBUG off); convenient by default on your own machine (DEBUG on).
DEBUG = os.environ.get("DEBUG", "0" if ON_RAILWAY else "1") == "1"

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is off.")
    SECRET_KEY = "dev-only-change-me"

# --- Hosts / CSRF -----------------------------------------------------------------------------
ALLOWED_HOSTS = _csv("ALLOWED_HOSTS", "localhost,127.0.0.1")
_railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")  # e.g. vibechat-production.up.railway.app
if _railway_domain and _railway_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_railway_domain)
if ON_RAILWAY and "healthcheck.railway.app" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("healthcheck.railway.app")  # Railway's deploy healthcheck uses this Host header

# Unless set explicitly, trust https://<each real host> (a leading dot means all subdomains).
CSRF_TRUSTED_ORIGINS = _csv("CSRF_TRUSTED_ORIGINS")
if not CSRF_TRUSTED_ORIGINS:
    for _h in ALLOWED_HOSTS:
        if _h in ("*", "localhost", "127.0.0.1", "healthcheck.railway.app"):
            continue
        CSRF_TRUSTED_ORIGINS.append(("https://*" if _h.startswith(".") else "https://") + _h)

# Railway terminates HTTPS at its edge and forwards plain HTTP plus X-Forwarded-Proto.
_HTTPS = (not DEBUG) and (ON_RAILWAY or os.environ.get("BEHIND_PROXY") == "1")
if _HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Apps -------------------------------------------------------------------------------------
INSTALLED_APPS = [
    "daphne", "channels",
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.accounts", "apps.social", "apps.chat",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Channel layer / cache --------------------------------------------------------------------
# Without REDIS_URL everything (channel layer, presence, calls, watch sessions) lives in one process's
# memory, so run exactly ONE Daphne process / ONE Railway replica. Add a Railway Redis service and set
# REDIS_URL to scale beyond that.
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [REDIS_URL]}}}
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# --- Database ---------------------------------------------------------------------------------
# Railway Postgres (reference ${{Postgres.DATABASE_URL}}) or any external Postgres such as Neon.
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    u = urlparse(DATABASE_URL)
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname,
        "PORT": u.port or 5432,
        "OPTIONS": dict(parse_qsl(u.query)),
        "DISABLE_SERVER_SIDE_CURSORS": True,  # needed behind a pooler (Neon -pooler host, pgbouncer)
        "CONN_MAX_AGE": 600,                  # reuse connections instead of reconnecting every request
        "CONN_HEALTH_CHECKS": True,
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

# --- Auth / sessions --------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"  # read sessions from memory, not the database
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/login/"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True

# --- Static files (WhiteNoise serves them in production) --------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"  # filled by `collectstatic` on every Railway start
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }

# --- Uploaded profile photos ------------------------------------------------------------------
# Railway's disk is wiped on every deploy. Attach a Volume to the service: Railway then exposes its
# mount path as RAILWAY_VOLUME_MOUNT_PATH and photos are kept in <volume>/media. MEDIA_ROOT overrides.
_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT") or (Path(_volume) / "media" if _volume else BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging: Railway shows stdout/stderr, so send errors there when DEBUG is off -------------
if not DEBUG:
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "WARNING")},
    }

# --- Video calls ------------------------------------------------------------------------------
# Browsers connect to each other; STUN helps them find a route. Railway cannot host a UDP TURN server,
# so use a hosted TURN provider (Metered, Twilio, Cloudflare...) with turn:/turns: over TCP/443.
# CALL_TURN_URLS is comma separated.
CALL_STUN_URLS = _csv("CALL_STUN_URLS", "stun:stun.l.google.com:19302")
CALL_TURN_URLS = _csv("CALL_TURN_URLS")
CALL_TURN_SECRET = os.environ.get("CALL_TURN_SECRET", "")        # coturn use-auth-secret (preferred)
CALL_TURN_USERNAME = os.environ.get("CALL_TURN_USERNAME", "")    # or a fixed username/credential pair
CALL_TURN_CREDENTIAL = os.environ.get("CALL_TURN_CREDENTIAL", "")
