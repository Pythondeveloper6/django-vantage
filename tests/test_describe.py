"""``manage.py vantage_describe``: a read-only, deterministic description of the admin."""

import ast
import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from tests.testapp.models import Author, Post
from vantage import site

pytestmark = pytest.mark.django_db


def run(*args):
    out = StringIO()
    call_command("vantage_describe", *args, stdout=out)
    return out.getvalue()


def described(*args):
    return json.loads(run(*args))


def test_json_shape_and_schema_version():
    data = described()
    assert data["schema_version"] == 1
    assert {"vantage_version", "site", "models", "errors"} <= set(data)
    post = next(m for m in data["models"] if m["model"] == "testapp.post")
    status = next(f for f in post["fields"] if f["name"] == "status")
    assert status["kind"] == "choice"
    assert {c["value"] for c in status["choices"]} >= {"draft"}
    author = next(f for f in post["fields"] if f["name"] == "author")
    assert author["relation"] == "testapp.author"
    assert post["api_url"].endswith("/testapp/post/")
    assert post["panel"]["inferred"] is True


def test_output_is_deterministic_and_sorted():
    assert run() == run()
    assert run("--format", "markdown") == run("--format", "markdown")
    labels = [m["model"] for m in described()["models"]]
    assert labels == sorted(labels)


def test_model_filter():
    data = described("--model", "testapp.post")
    assert [m["model"] for m in data["models"]] == ["testapp.post"]
    with pytest.raises(CommandError):
        run("--model", "nope.nothing")


def test_undeclared_lists_only_inferred_panels_with_valid_attributes():
    data = described("--undeclared")
    labels = {m["model"] for m in data["models"]}
    assert "testapp.archive" not in labels  # has a hand-written panel
    assert "testapp.post" in labels
    for item in data["models"]:
        assert item["panel"]["inferred"]
        assert item["inferred_defaults"]
        for line in item["inferred_defaults"]:
            tree = ast.parse(line)
            assert isinstance(tree.body[0], ast.Assign)
    assert "inferred_defaults" not in next(
        m for m in described()["models"] if m["model"] == "testapp.archive"
    )
    assert described()["models"]  # the full run still has hand-written ones


def test_sensitive_fields_are_named_never_valued():
    secret = "hunter2-very-secret-value"
    Author.objects.create(name="Ada", email="ada@example.test", bio=secret)
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_user("zed", password=secret)
    text = run() + run("--format", "markdown")
    assert secret not in text
    user = next(m for m in described()["models"] if m["model"] == "auth.user")
    assert {"name": "password", "masked": True} in user["sensitive_fields"]
    assert "password (masked)" in run("--model", "auth.user", "--format", "markdown")


def test_secret_looking_settings_are_omitted():
    with override_settings(
        VANTAGE={
            "SITE_TITLE": "Shop admin",
            "OIDC_CLIENT_SECRET": "s3cr3t-value",
            "WEBHOOKS_SIGNING_KEY": "k3y-value",
        }
    ):
        from vantage.conf import vantage_settings

        vantage_settings.reset()
        try:
            text = run()
        finally:
            vantage_settings.reset()
    assert "s3cr3t-value" not in text and "k3y-value" not in text
    assert "OIDC_CLIENT_SECRET" not in text
    assert "Shop admin" in text


def test_no_row_data_is_read():
    Post.objects.all().count()  # warm nothing; just make sure the table exists
    with CaptureQueriesContext(connection) as queries:
        run()
        run("--undeclared", "--format", "markdown")
    tables = {m._meta.db_table for m in (p.model for p in site.panels())}
    for query in queries.captured_queries:
        sql = query["sql"].lower()
        assert not any(f'from "{table}"' in sql for table in tables), sql


def test_markdown_renders():
    text = run("--format", "markdown", "--model", "testapp.post")
    assert text.startswith("# Vantage admin description")
    assert "## testapp.post" in text
    assert "| Field | Kind |" in text
    assert "```python" in text


def test_one_broken_model_is_reported_and_the_rest_described(monkeypatch):
    from vantage import tenancy

    real = tenancy.scope_for

    def scope_for(model):
        if model is Post:
            raise RuntimeError("table is missing")
        return real(model)

    monkeypatch.setattr(tenancy, "scope_for", scope_for)
    data = described()
    assert data["errors"] == [{"model": "testapp.post", "error": "RuntimeError: table is missing"}]
    labels = {m["model"] for m in data["models"]}
    assert "testapp.post" not in labels and "testapp.author" in labels


def test_upgrade_check_json_carries_the_agent_keys():
    out = StringIO()
    try:
        call_command(
            "vantage_upgrade_check",
            "--skip-database",
            "--format",
            "json",
            "--from",
            "0.2.0a1",
            stdout=out,
        )
    except SystemExit:
        pass
    payload = json.loads(out.getvalue())
    assert payload["schema_version"] == 1
    assert payload["from_version"] == "0.2.0a1"
    assert payload["to_version"] == payload["version"]
    assert all(isinstance(line, str) for line in payload["note_lines"])
    assert isinstance(payload["deprecations"], list)


def test_a_credential_inside_a_url_value_is_masked():
    from vantage.describe import _jsonable

    assert _jsonable("postgres://user:pw@db.example/app") == "postgres://***@db.example/app"
    assert _jsonable({"plain": "https://example.com/x"}) == {"plain": "https://example.com/x"}
