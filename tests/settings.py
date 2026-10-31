"""Minimal Django settings for the test suite."""

import os
import tempfile
from pathlib import Path

SECRET_KEY = "vantage-test-key-not-secret"
DEBUG = False
USE_TZ = True

# SQLite in memory unless VANTAGE_TEST_DB names a server, which is how the CI database
# jobs run the same suite against PostgreSQL and MySQL. Connection details come from
# the usual environment variables so a local container works without editing this.
_TEST_DB = os.environ.get("VANTAGE_TEST_DB", "sqlite")
if _TEST_DB == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "vantage"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }
elif _TEST_DB == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "vantage"),
            "USER": os.environ.get("MYSQL_USER", "root"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", "root"),
            "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
            "TEST": {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

if _TEST_DB != "sqlite":
    # The test app has no migrations, so its tables are created by syncdb *before*
    # contrib.auth's migrations run — fine on SQLite, which checks foreign keys late, but
    # a missing "auth_user" on a server. Building every app's tables from its models in
    # one pass sidesteps the ordering; migrations themselves are exercised on SQLite.
    class _NoMigrations:
        def __contains__(self, item):
            return True

        def __getitem__(self, item):
            return None

    MIGRATION_MODULES = _NoMigrations()

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
