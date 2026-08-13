"""Settings for the django-vantage demo.

This is a demo, not a template for production: the secret key is in the file, DEBUG is
on, and the database is a SQLite file next to this project. Everything here exists to
be read, so it stays as short as a working Django project can be.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-vantage-example-do-not-use-in-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "vantage",
    "shop",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The panel sends anonymous visitors here, and sends them back to the dashboard once
# they are in.
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

VANTAGE = {
    "SITE_TITLE": "Northwind Supply",
    "SITE_ACRONYM": "NS",
    "BRAND_COLOR": "#0d9488",
    "BRAND_RADIUS": "0.625rem",
    "THEME": "system",
    "ENVIRONMENT": "Demo data — nothing here is real",
    # The sidebar, stated rather than derived: groups in the order you want them,
    # items in the order you want them, renamed where the model name is not the word
    # your team uses, plus links to anywhere else.
    #
    # Declaring a nav can reorder, rename and hide — it can never grant access. Every
    # entry is still filtered through the permission layer, so signing in as `editor`
    # shows a shorter sidebar than `admin` from this same declaration.
    "NAV": [
        {"route": "vantage:dashboard", "label": "Overview", "icon": "layout-dashboard"},
        {
            "group": "Commerce",
            "items": [
                {"model": "shop.Order", "badge": "live"},
                {"model": "shop.Product", "label": "Catalogue"},
                "shop.Category",
                "shop.Tag",
            ],
        },
        {
            "group": "People",
            "items": [
                {"model": "shop.Customer", "icon": "users"},
                {"model": "shop.Review", "label": "Feedback"},
            ],
        },
        {
            "group": "Elsewhere",
            "icon": "shield",
            "items": [
                {"url": "https://docs.djangoproject.com/", "label": "Django docs"},
            ],
        },
    ],
    # Anything the declaration does not mention still appears, so registering a new
    # model never makes it silently unreachable. Set this to "hide" for a strict nav.
    "NAV_UNLISTED": "append",
    # Comment this out and the dashboard generates itself from the registry instead.
    "DASHBOARD": "shop.dashboard.ShopDashboard",
}
