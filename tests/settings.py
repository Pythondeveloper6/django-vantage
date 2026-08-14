"""Minimal Django settings for the test suite."""

import tempfile
from pathlib import Path

SECRET_KEY = "vantage-test-key-not-secret"
DEBUG = False
USE_TZ = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "vantage",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

ROOT_URLCONF = "tests.urls"
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
# Unset, MEDIA_ROOT is "" and every upload lands relative to the working directory —
# so a test that saved a logo wrote vantage/branding/ into the repository root, and
# three of those had already been committed-adjacent before anyone noticed. Point it
# at a temporary directory instead: tests should not be able to write into the tree
# they are testing.
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="vantage-test-media-"))
