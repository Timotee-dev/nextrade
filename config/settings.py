"""Django settings. Every secret and environment-specific value comes from .env."""

from pathlib import Path
import os

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    """Environment value, treating an empty value (e.g. `DATABASE_URL=` in .env) as unset."""
    value = os.getenv(name)
    return value if value not in (None, "") else default


DEBUG = env("DJANGO_DEBUG", "true").lower() == "true"
SECRET_KEY = env("DJANGO_SECRET_KEY") or ("dev-only-insecure-key" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG=false")
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.market",
    "apps.ops",
    "apps.research",
    "apps.trading",
    "apps.dashboard",
    "apps.notify",
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
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# SQLite works for Phases 0-1. Phase 2 (market data) switches to PostgreSQL via DATABASE_URL.
DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"), conn_max_age=60
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"  # all storage and logic in UTC; display converts to Africa/Lagos
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
LOGIN_URL = "/admin/login/"
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
}

# --- NEXTRADE ---------------------------------------------------------------
NEXTRADE = {
    "BINANCE_BASE_URL": env("BINANCE_BASE_URL", "https://api.binance.com"),
    "BINANCE_TESTNET_URL": env("BINANCE_TESTNET_URL", "https://testnet.binance.vision"),
    "VENUE": env("NEXTRADE_VENUE", "quidax"),
    "QUIDAX_BASE_URL": env("QUIDAX_BASE_URL", "https://openapi.quidax.io/exchange-open-api/api/v1"),
    # Tightest NGN books seen on 2026-11-11 first; BTC/NGN and SOL/NGN spreads were near 0.5%.
    "QUIDAX_SYMBOLS": env("QUIDAX_SYMBOLS", "ETHNGN,USDTNGN,BTCNGN,SOLNGN,BTCUSDT,ETHUSDT").split(","),
    "DEFAULT_SYMBOLS": env(
        "NEXTRADE_SYMBOLS",
        "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,TRXUSDT,LINKUSDT,AVAXUSDT",
    ).split(","),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
