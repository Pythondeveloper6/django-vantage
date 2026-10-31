"""``manage.py vantage_openapi`` - write the JSON API's OpenAPI 3.1 document.

``--user`` describes the API as that person may use it: only models they can view, and
only the verbs they hold. Without it the document covers **every registered panel** as a
superuser would see it, which reveals the structure of models some staff cannot see -
keep that output out of public places.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory


class Command(BaseCommand):
    help = "Emit the OpenAPI 3.1 document for the JSON API."

    def add_arguments(self, parser):
        parser.add_argument("--output", "-o", help="Write to this file instead of stdout.")
        parser.add_argument(
            "--user",
            help="Describe the API as this username sees it. Default: every panel "
            "(reveals model structure).",
        )

    def handle(self, *args, **options):
        from vantage import site
        from vantage.apiopenapi import build_document, synthetic_superuser_request

        username = options["user"]
        if username:
            User = get_user_model()
            try:
                user = User._default_manager.get_by_natural_key(username)
            except User.DoesNotExist as exc:
                raise CommandError(f"No such user: {username}") from exc
            request = RequestFactory().get("/")
            request.user = user
        else:
            request = synthetic_superuser_request()

        try:
            document = build_document(site, request)
        except Exception as exc:  # NoReverseMatch when the API is not mounted, etc.
            raise CommandError(f"Could not build the document: {exc}") from exc
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if options["output"]:
            Path(options["output"]).write_text(text, encoding="utf-8")
            self.stdout.write(f"Wrote {options['output']}")
        else:
            self.stdout.write(text)
