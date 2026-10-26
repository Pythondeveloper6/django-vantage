"""Sensitive fields: what is inferred, and that no surface leaks a masked value.

The surface tests share one shape on purpose. Each renders a real response for a
record whose secrets are long, distinctive strings, and asserts that none of them
appears anywhere in the body — not in a cell, an attribute, a ``data-`` blob or a JSON
payload. Asserting on the whole body rather than on one element is the point: a leak
is a leak wherever in the response it lands.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.checks import Error, Warning
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, ZCredential
from vantage import importer, inference, sensitive, site
from vantage.models import VantageLogEntry
from vantage.panels import ModelPanel
from vantage.sensitive import MASK

TOKEN = "sk_live_TOKENVALUE0123456789"
CARD = "4242424242424242"
PIN = "938271"
PIN_HASH = "pbkdf2_sha256$720000$saltysalt$HASHEDPINVALUE="
KEY = "-----BEGIN PRIVATE KEY-----MIIEvQIBADANPRIVATEKEYBODY"
SECRETS = (TOKEN, CARD, PIN, PIN_HASH, KEY, "PRIVATEKEYBODY", "TOKENVALUE")


def leaks(text: str | bytes) -> list[str]:
    """Every secret that appears in ``text``. Empty means nothing leaked."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return [secret for secret in SECRETS if secret in text]


def content(response) -> str:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content).decode()
    return response.content.decode()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def panel():
    return site.get_panel(ZCredential)


@pytest.fixture(autouse=True)
def _clean_panel(panel):
    """Every test may reconfigure the shared panel; none may leak config to the next."""
    saved = dict(panel.__dict__)
    yield
    panel.__dict__.clear()
    panel.__dict__.update(saved)


@pytest.fixture(autouse=True)
def _fresh_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def client_in(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def cred(db):
    owner = Author.objects.create(name="Ada Owner", email="ada@example.com")
    return ZCredential.objects.create(
        name="Production key",
        owner=owner,
        api_token=TOKEN,
        card_number=CARD,
        pin=PIN,
        pin_hash=PIN_HASH,
        private_key=KEY,
    )


def staff_with(*codenames, model=ZCredential):
    user = User.objects.create_user("clerk", "clerk@example.com", "x", is_staff=True)
    for codename in codenames:
        if codename == "reveal_sensitive":
            sensitive.ensure_reveal_permission()
            perm = Permission.objects.get(codename="reveal_sensitive")
        else:
            ct = ContentType.objects.get_for_model(model)
            perm = Permission.objects.get(content_type=ct, codename=codename)
        user.user_permissions.add(perm)
    return User.objects.get(pk=user.pk)


def list_url():
    return reverse("vantage:testapp-zcredential-list")


def detail_url(obj):
    return reverse("vantage:testapp-zcredential-detail", args=[obj.pk])


def detail_url_for_user(user):
    return reverse("vantage:auth-user-detail", args=[user.pk])


def reveal_url(obj, field):
    return reverse("vantage:testapp-zcredential-reveal", args=[obj.pk, field])


# ── inference ───────────────────────────────────────────────────────────────


class TestInference:
    def test_the_sensitive_columns_are_found_with_no_configuration(self):
        assert sensitive.infer_sensitive_fields(ZCredential) == [
            "api_token",
            "card_number",
            "pin",
            "pin_hash",
            "private_key",
        ]

    @pytest.mark.parametrize(
        "name",
        [
            "token_type",  # a choice, and a metadata suffix
            "tokenizer_name",  # "token" is not a whole word here
            "is_secretary",  # nor is "secret"
            "passport_photo_url",  # personal data, not a credential
            "otp_enabled",  # a boolean
            "pin_attempts",  # a counter
            "password_changed_at",  # a date
            "owner",  # a relation
        ],
    )
    def test_the_near_misses_stay_visible(self, name):
        assert name not in sensitive.infer_sensitive_fields(ZCredential)

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("api_token", True),
            ("token", True),
            ("stripe_api_key", True),
            ("stripeApiKey", True),
            ("social_security_number", True),
            ("customer_iban", True),
            ("cvc", True),
            ("tokenizer_name", False),
            ("is_secretary", False),
            ("passport_photo_url", False),
            ("token_type", False),
            ("password_hint", False),
            ("api_keyboard", False),
            ("", False),
        ],
    )
    def test_names_match_by_whole_word(self, name, expected):
        assert sensitive.name_is_sensitive(name) is expected

    def test_an_encrypted_field_class_is_sensitive_whatever_it_is_called(self):
        class EncryptedCharField(models.CharField):
            pass

        field = EncryptedCharField(max_length=10)
        field.name = "notes"
        assert sensitive.is_encrypted_field(field)
        assert sensitive.field_is_sensitive(field)

    def test_a_subclass_of_an_encrypted_field_is_still_encrypted(self):
        class EncryptedTextField(models.TextField):
            pass

        class MyNotes(EncryptedTextField):
            pass

        assert sensitive.is_encrypted_field(MyNotes())

    def test_the_user_password_is_sensitive_and_hashed(self):
        field = User._meta.get_field("password")
        assert sensitive.field_is_sensitive(field)
        assert sensitive.is_hashed(User, "password")

    @pytest.mark.parametrize(
        ("name", "hashed"),
        [("pin_hash", True), ("api_key_digest", True), ("content_hash", False), ("hash", False)],
    )
    def test_only_a_hash_of_a_secret_is_unrevealable(self, name, hashed):
        assert sensitive.is_hashed(ZCredential, name) is hashed

    def test_inference_keeps_secrets_out_of_columns_filters_and_search(self):
        assert "api_token" not in inference.infer_list_display(ZCredential)
        assert "api_token" not in inference.infer_list_search(ZCredential)
        assert "pin" not in inference.infer_list_filter(ZCredential)

    def test_infer_all_reports_them_for_the_scaffold(self):
        assert inference.infer_all(ZCredential)["sensitive_fields"] == [
            "api_token",
            "card_number",
            "pin",
            "pin_hash",
            "private_key",
        ]

    def test_the_scaffold_writes_the_line(self):
        from vantage.scaffold import render_panel_class

        rendered = render_panel_class(ZCredential)
        assert "sensitive_fields = [" in rendered
        assert '"api_token"' in rendered

    def test_the_scaffold_says_nothing_for_a_model_without_secrets(self):
        from vantage.scaffold import render_panel_class

        assert "sensitive_fields" not in render_panel_class(Author)


class TestVocabularySetting:
    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": ["nhs_number"]})
    def test_a_list_replaces_the_vocabulary(self):
        assert sensitive.name_is_sensitive("patient_nhs_number")
        assert not sensitive.name_is_sensitive("api_token")

    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": ["...", "nhs_number"]})
    def test_an_ellipsis_keeps_the_defaults(self):
        assert sensitive.name_is_sensitive("patient_nhs_number")
        assert sensitive.name_is_sensitive("api_token")

    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": [..., "nhs_number"]})
    def test_the_python_ellipsis_works_too(self):
        assert sensitive.name_is_sensitive("api_token")

    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": "password"})
    def test_a_bare_string_is_refused_rather_than_iterated(self):
        with pytest.raises(ImproperlyConfigured):
            sensitive.patterns()

    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": []})
    def test_an_empty_list_switches_name_matching_off(self, panel):
        assert sensitive.infer_sensitive_fields(ZCredential) == []

    @override_settings(VANTAGE={"SENSITIVE_PATTERNS": ["...", "tokenizer_name"]})
    def test_the_panel_follows_a_changed_vocabulary(self, panel):
        assert "tokenizer_name" in panel.get_sensitive_fields()


# ── masking ─────────────────────────────────────────────────────────────────


class TestMask:
    def test_the_mask_is_the_same_width_for_any_value(self):
        assert sensitive.mask("a") == sensitive.mask("a" * 400) == MASK

    def test_a_card_keeps_its_last_four(self):
        assert sensitive.mask(CARD, "card_number") == "•••• 4242"

    def test_a_short_card_like_value_is_fully_masked(self):
        assert sensitive.mask("4242", "card_number") == MASK

    def test_last_four_is_only_for_card_like_names(self):
        assert sensitive.mask(TOKEN, "api_token") == MASK


# ── configuration ───────────────────────────────────────────────────────────


class TestPanelConfiguration:
    def test_an_explicit_list_replaces_inference(self, panel):
        panel.sensitive_fields = ["tokenizer_name"]
        assert panel.get_sensitive_fields() == frozenset({"tokenizer_name"})

    def test_an_empty_list_switches_masking_off(self, panel, client_in, cred):
        panel.sensitive_fields = []
        assert panel.get_sensitive_fields() == frozenset()
        assert TOKEN in client_in.get(detail_url(cred)).content.decode()

    def test_the_user_password_is_masked_even_when_a_panel_says_otherwise(self):
        user_panel = site.get_panel(User)
        user_panel.sensitive_fields = []
        try:
            assert "password" in user_panel.get_sensitive_fields()
        finally:
            del user_panel.sensitive_fields

    def test_an_explicitly_searched_secret_is_dropped(self, panel):
        panel.list_search = ["name", "api_token", "owner__name"]
        assert panel.get_list_search() == ["name", "owner__name"]

    def test_an_explicitly_filtered_secret_is_dropped(self, panel):
        panel.list_filter = ["token_type", "pin"]
        assert panel.get_list_filter() == ["token_type"]

    def test_an_explicit_ordering_by_a_secret_is_dropped(self, panel):
        panel.ordering = ["api_token", "-created_at"]
        assert panel.get_ordering() == ["-created_at"]

    def test_a_relation_path_into_a_secret_is_dropped(self, db):
        from vantage.sites import VantageSite

        local = VantageSite(name="sensitive-path")
        local.register(ZCredential)

        class AuthorPanel(ModelPanel):
            list_search = ["name", "credentials__api_token"]

        local.register(Author, AuthorPanel)
        assert local.get_panel(Author).get_list_search() == ["name"]

    def test_a_declared_secret_under_an_innocent_name_leaves_the_inferred_columns(self, panel):
        panel.sensitive_fields = ["tokenizer_name"]
        assert "tokenizer_name" not in panel.get_list_display()


class TestChecks:
    def test_an_explicit_search_on_a_secret_warns(self, panel):
        panel.list_search = ["name", "api_token"]
        found = [m for m in panel.check() if m.id == "vantage.W090"]
        assert len(found) == 1 and isinstance(found[0], Warning)
        assert "api_token" in found[0].msg

    def test_search_fields_is_named_as_the_author_wrote_it(self):
        class Configured(ModelPanel):
            search_fields = ["api_token"]

        found = [m for m in Configured(ZCredential).check() if m.id == "vantage.W090"]
        assert "'search_fields'" in found[0].msg

    def test_an_explicit_filter_on_a_secret_warns(self, panel):
        panel.list_filter = ["pin"]
        assert any(m.id == "vantage.W090" for m in panel.check())

    def test_inferred_configuration_never_warns(self, panel):
        assert not [m for m in panel.check() if m.id == "vantage.W090"]

    def test_an_unknown_sensitive_field_is_an_error(self, panel):
        panel.sensitive_fields = ["api_tokn"]
        found = [m for m in panel.check() if m.id == "vantage.E090"]
        assert len(found) == 1 and isinstance(found[0], Error)

    def test_a_string_instead_of_a_list_is_an_error(self, panel):
        panel.sensitive_fields = "api_token"
        assert any(m.id == "vantage.E090" for m in panel.check())


# ── every surface ───────────────────────────────────────────────────────────


class TestListPage:
    def test_a_declared_secret_column_renders_masked(self, panel, client_in, cred):
        panel.list_display = ["name", "api_token", "card_number", "pin", "owner"]
        body = client_in.get(list_url()).content.decode()
        assert not leaks(body)
        assert MASK in body
        assert "•••• 4242" in body
        assert reveal_url(cred, "api_token") in body

    def test_a_masked_column_is_not_sortable(self, panel, client_in, cred):
        panel.list_display = ["name", "api_token"]
        response = client_in.get(list_url())
        header = next(c for c in response.context["columns"] if c["name"] == "api_token")
        assert header["sortable"] is False

    def test_sorting_by_a_secret_is_ignored(self, panel, client_in, cred):
        panel.list_display = ["name", "api_token"]
        response = client_in.get(list_url(), {"sort": "api_token"})
        assert response.context["sort"] is None

    def test_searching_for_a_secret_finds_nothing(self, panel, client_in, cred):
        panel.list_search = ["name", "api_token"]
        response = client_in.get(list_url(), {"q": "TOKENVALUE"})
        assert response.context["row_count"].value == 0

    def test_filtering_by_a_secret_is_impossible(self, panel, client_in, cred):
        panel.list_filter = ["pin", "token_type"]
        response = client_in.get(list_url(), {"pin": PIN})
        assert [f.path for f in response.context["filters"]] == ["token_type"]

    def test_the_summary_bar_never_aggregates_a_secret(self, panel, client_in, cred):
        cred.pin_attempts = 987654
        cred.save()
        panel.sensitive_fields = ["api_token", "pin_attempts"]
        panel.list_display = ["name", "pin_attempts"]
        panel.list_aggregates = {"pin_attempts": "max"}
        response = client_in.get(list_url())
        body = response.content.decode()
        assert "987654" not in body and "987,654" not in body
        assert "pin_attempts" not in response.context["summary"]

    def test_inline_editing_never_offers_a_secret(self, panel, cred):
        from vantage.inline import inline_fields

        panel.list_display = ["name", "api_token", "tokenizer_name"]
        panel.list_editable = ["api_token", "tokenizer_name"]
        assert [f.name for f in inline_fields(panel)] == ["tokenizer_name"]

    def test_masking_costs_no_queries(self, panel, client_in, cred, django_assert_num_queries):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        panel.list_display = ["name", "tokenizer_name"]
        with CaptureQueriesContext(connection) as plain:
            client_in.get(list_url())
        panel.list_display = ["name", "api_token"]
        with django_assert_num_queries(len(plain.captured_queries)):
            client_in.get(list_url())


class TestDetailPage:
    def test_secrets_render_masked_with_a_reveal_control(self, client_in, cred):
        body = client_in.get(detail_url(cred)).content.decode()
        assert not leaks(body)
        assert reveal_url(cred, "api_token") in body
        # A hash is shown masked but never offered for reveal.
        assert reveal_url(cred, "pin_hash") not in body

    def test_the_near_misses_render_in_full(self, client_in, cred):
        cred.tokenizer_name = "bert-base"
        cred.save()
        assert "bert-base" in client_in.get(detail_url(cred)).content.decode()

    def test_a_str_that_prints_a_secret_is_replaced_in_the_title(self, client_in, cred):
        import re

        cred.name = TOKEN  # __str__ returns name, so the title would be the token
        cred.save()
        body = client_in.get(detail_url(cred)).content.decode()
        title = re.search(r"<title>(.*?)</title>", body, re.S).group(1)
        heading = re.search(r'<h1 class="v-page-title">(.*?)</h1>', body, re.S).group(1)
        crumbs = re.search(r"<nav[^>]*breadcrumb.*?</nav>", body, re.S | re.I)
        assert not leaks(title) and not leaks(heading)
        assert crumbs is None or not leaks(crumbs.group(0))
        assert f"Z credential #{cred.pk}" in heading

    def test_the_audit_history_never_prints_a_secret(self, client_in, cred):
        VantageLogEntry.objects.create(
            action=VantageLogEntry.Action.UPDATE,
            content_type=ContentType.objects.get_for_model(ZCredential),
            object_id=str(cred.pk),
            object_repr="Production key",
            # An entry written before masking existed, carrying the raw value.
            changes={"api_token": {"from": "old", "to": TOKEN}},
        )
        body = client_in.get(detail_url(cred)).content.decode()
        assert not leaks(body)
        assert "value not recorded" in body

    def test_the_audit_page_never_prints_a_secret(self, client_in, cred):
        VantageLogEntry.objects.create(
            action=VantageLogEntry.Action.UPDATE,
            content_type=ContentType.objects.get_for_model(ZCredential),
            object_id=str(cred.pk),
            changes={"private_key": {"from": KEY, "to": "x"}},
        )
        assert not leaks(client_in.get(reverse("vantage:audit")).content)

    def test_a_related_chip_does_not_print_a_secret_from_str(self, client_in, cred):
        """The owner's __str__ is its name; make the name equal a secret of the owner's."""
        author_panel = site.get_panel(Author)
        author_panel.sensitive_fields = ["email"]
        try:
            owner = cred.owner
            owner.name = owner.email = "owner-SECRETMAIL@example.com"
            owner.save()
            body = client_in.get(detail_url(cred)).content.decode()
            assert "SECRETMAIL" not in body
            assert f"Author #{owner.pk}" in body
        finally:
            del author_panel.sensitive_fields


class TestExport:
    @pytest.mark.parametrize("fmt", ["csv", "tsv", "json"])
    def test_an_export_carries_the_mask_not_the_value(self, panel, client_in, cred, fmt):
        panel.list_display = ["name", "api_token", "card_number", "private_key"]
        response = client_in.get(list_url(), {"export": fmt})
        text = content(response)
        assert not leaks(text)
        assert MASK in text and "•••• 4242" in text

    def test_an_empty_secret_exports_empty(self, panel, client_in, cred):
        cred.api_token = ""
        cred.save()
        panel.list_display = ["name", "api_token"]
        rows = list(csv.reader(io.StringIO(content(client_in.get(list_url(), {"export": "csv"})))))
        assert rows[1] == ["Production key", ""]


class TestApi:
    def api(self, *parts):
        return reverse(*parts)

    def test_the_list_masks_a_requested_secret(self, client_in, cred):
        url = reverse(
            "vantage:api-list", kwargs={"app_label": "testapp", "model_name": "zcredential"}
        )
        response = client_in.get(url, {"fields": "name,api_token,pin"})
        assert response.status_code == 200
        assert not leaks(response.content)
        cell = response.json()["results"][0]["cells"]["api_token"]
        assert cell == {"value": None, "display": MASK, "masked": True}

    def test_the_list_refuses_to_sort_by_a_secret(self, client_in, cred):
        url = reverse(
            "vantage:api-list", kwargs={"app_label": "testapp", "model_name": "zcredential"}
        )
        response = client_in.get(url, {"fields": "name,api_token", "sort": "api_token"})
        assert response.json()["sort"] is None

    def test_the_detail_masks_every_secret(self, client_in, cred):
        url = reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "zcredential", "pk": cred.pk},
        )
        response = client_in.get(url)
        assert not leaks(response.content)
        fields = response.json()["fields"]
        assert fields["api_token"]["masked"] is True and fields["api_token"]["value"] is None
        assert fields["tokenizer_name"].get("masked") is None

    def test_the_schema_marks_secrets_write_only(self, client_in, cred):
        response = client_in.get(reverse("vantage:api-schema"))
        model = response.json()["models"]["testapp.zcredential"]
        by_name = {f["name"]: f for f in model["fields"]}
        assert by_name["api_token"]["sensitive"] is True
        assert by_name["name"]["sensitive"] is False
        assert "api_token" not in model["list_search"]

    def _patch(self, client, cred, fields):
        url = reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "zcredential", "pk": cred.pk},
        )
        return client.patch(url, json.dumps({"fields": fields}), content_type="application/json")

    def test_an_empty_string_keeps_the_secret(self, client_in, cred):
        response = self._patch(client_in, cred, {"name": "Renamed", "api_token": ""})
        assert response.status_code == 200, response.content
        cred.refresh_from_db()
        assert cred.api_token == TOKEN and cred.name == "Renamed"
        assert not leaks(response.content)

    def test_null_clears_a_nullable_secret(self, client_in, cred):
        assert self._patch(client_in, cred, {"pin": None}).status_code == 200
        cred.refresh_from_db()
        assert cred.pin is None

    def test_a_new_value_is_written(self, client_in, cred):
        assert self._patch(client_in, cred, {"api_token": "sk_new"}).status_code == 200
        cred.refresh_from_db()
        assert cred.api_token == "sk_new"

    def test_a_put_that_omits_a_secret_keeps_it(self, client_in, cred):
        url = reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "zcredential", "pk": cred.pk},
        )
        payload = {"fields": {"name": "Put", "token_type": "bearer", "pin_attempts": 0}}
        response = client_in.put(url, json.dumps(payload), content_type="application/json")
        assert response.status_code == 200, response.content
        cred.refresh_from_db()
        assert cred.api_token == TOKEN and cred.pin == PIN

    def test_a_conflict_never_shows_their_secret(self, cred):
        from vantage.concurrency import field_diff

        diff = field_diff(cred, {"api_token": "mine"})
        assert diff["api_token"]["theirs"] is None
        assert diff["api_token"]["theirs_display"] == MASK


class TestGlobalSearchAndAutocomplete:
    def test_search_never_matches_or_prints_a_secret(self, panel, client_in, cred):
        panel.list_search = ["name", "api_token"]
        response = client_in.get(reverse("vantage:search-json"), {"q": "TOKENVALUE"})
        assert response.json()["total"] == 0

    def test_a_hit_whose_str_prints_a_secret_is_relabelled(self, client_in, cred):
        cred.name = TOKEN
        cred.tokenizer_name = "findme"
        cred.save()
        site.get_panel(ZCredential).list_search = ["tokenizer_name"]
        response = client_in.get(reverse("vantage:search-json"), {"q": "findme"})
        assert response.json()["total"] == 1
        assert not leaks(response.content)

    def test_the_search_page_never_prints_a_secret(self, client_in, cred):
        cred.name = TOKEN
        cred.tokenizer_name = "findme"
        cred.save()
        site.get_panel(ZCredential).list_search = ["tokenizer_name"]
        assert not leaks(client_in.get(reverse("vantage:search"), {"q": "findme"}).content)

    def test_autocomplete_labels_never_print_a_secret(self, client_in, cred):
        cred.name = TOKEN
        cred.save()
        response = client_in.get(reverse("vantage:autocomplete"), {"model": "testapp.ZCredential"})
        assert response.status_code == 200
        assert not leaks(response.content)

    def test_autocomplete_never_searches_a_secret(self, panel, client_in, cred):
        panel.list_search = ["api_token"]
        response = client_in.get(
            reverse("vantage:autocomplete"), {"model": "testapp.ZCredential", "q": "TOKENVALUE"}
        )
        assert response.json()["results"] == []


class TestEditForm:
    def edit_url(self, cred):
        return reverse("vantage:testapp-zcredential-change", args=[cred.pk])

    def data(self, cred, **overrides):
        values = {
            "name": cred.name,
            "owner": cred.owner_id,
            "token_type": cred.token_type,
            "tokenizer_name": cred.tokenizer_name,
            "passport_photo_url": "",
            "pin_attempts": cred.pin_attempts,
            "api_token": "",
            "card_number": "",
            "pin": "",
            "private_key": "",
        }
        values.update(overrides)
        return values

    def test_the_form_never_renders_a_stored_secret(self, client_in, cred):
        body = client_in.get(self.edit_url(cred)).content.decode()
        assert not leaks(body)
        assert 'name="api_token"' in body
        assert "Unchanged" in body
        assert 'autocomplete="new-password"' in body

    def test_a_hash_is_not_an_input(self, client_in, cred):
        body = client_in.get(self.edit_url(cred)).content.decode()
        assert 'name="pin_hash"' not in body

    def test_an_empty_submission_keeps_every_secret(self, client_in, cred):
        response = client_in.post(self.edit_url(cred), self.data(cred, name="Renamed"))
        assert response.status_code == 302, response.context and response.context["form"].errors
        cred.refresh_from_db()
        assert (cred.name, cred.api_token, cred.pin, cred.private_key) == (
            "Renamed",
            TOKEN,
            PIN,
            KEY,
        )

    def test_a_new_value_replaces_the_secret(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred, api_token="sk_rotated"))
        cred.refresh_from_db()
        assert cred.api_token == "sk_rotated"

    def test_the_clear_box_empties_a_nullable_secret(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred, pin__clear="1"))
        cred.refresh_from_db()
        assert cred.pin is None

    def test_the_clear_box_blanks_a_blank_only_secret(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred, private_key__clear="1"))
        cred.refresh_from_db()
        assert cred.private_key == ""

    def test_a_failed_save_does_not_echo_what_was_typed(self, client_in, cred):
        response = client_in.post(
            self.edit_url(cred), self.data(cred, name="", api_token="sk_typed_SECRETECHO")
        )
        assert response.status_code == 200
        assert "SECRETECHO" not in response.content.decode()

    def test_the_audit_entry_records_the_change_and_not_the_value(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred, api_token="sk_rotated"))
        entry = VantageLogEntry.objects.filter(action="update").latest("pk")
        assert entry.changes == {"api_token": {"redacted": True}}

    def test_keeping_a_secret_writes_no_audit_noise(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred))
        assert not VantageLogEntry.objects.filter(action="update").exists()

    def test_a_clear_is_audited_as_a_redacted_change(self, client_in, cred):
        client_in.post(self.edit_url(cred), self.data(cred, pin__clear="1"))
        entry = VantageLogEntry.objects.filter(action="update").latest("pk")
        assert entry.changes == {"pin": {"redacted": True}}

    def test_a_delete_snapshot_skips_secrets(self, client_in, cred):
        client_in.post(reverse("vantage:testapp-zcredential-delete", args=[cred.pk]))
        entry = VantageLogEntry.objects.filter(action="delete").latest("pk")
        assert not leaks(json.dumps(entry.changes))
        assert "api_token" not in entry.changes


class TestDuplicateAndBulkEdit:
    def test_the_duplicate_page_never_carries_a_secret(self, client_in, cred):
        url = reverse("vantage:testapp-zcredential-duplicate", args=[cred.pk])
        response = client_in.get(url)
        assert response.status_code == 200
        assert not leaks(response.content)

    def test_the_plan_does_not_copy_secrets_and_says_so(self, panel, cred):
        from vantage.records import duplicate_plan

        plan = duplicate_plan(panel, cred)
        assert "api_token" not in plan.initial
        assert any("not copied" in note for note in plan.notes)

    def test_bulk_edit_never_offers_a_secret(self, panel):
        from vantage.records import bulk_edit_fields

        offered = bulk_edit_fields(panel)
        assert "api_token" not in offered and "pin" not in offered
        assert "tokenizer_name" in offered


class TestImport:
    def test_the_preview_masks_both_sides_of_a_changed_secret(self, panel, cred):
        raw = f"id,name,api_token\n{cred.pk},Production key,sk_live_NEWVALUE_999\n".encode()
        parsed = importer.read_csv(raw)
        options = importer.importable_fields(panel)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            importer.infer_mapping(parsed.headers, options),
            key_field="id",
            mode="update",
        )
        result = importer.dry_run(panel, parsed, mapping)
        rendered = json.dumps(
            [[c.before, c.after] for d in result.diffs for c in d.changes], ensure_ascii=False
        )
        assert not leaks(rendered) and "NEWVALUE" not in rendered
        assert MASK in rendered

    def test_a_secret_can_never_be_the_upsert_key(self, panel):
        options = importer.importable_fields(panel)
        assert "api_token" not in [o.name for o in importer.key_options(options)]


# ── reveal ──────────────────────────────────────────────────────────────────


class TestReveal:
    def test_a_superuser_gets_the_value_uncached(self, client_in, cred):
        response = client_in.post(reveal_url(cred, "api_token"))
        assert response.status_code == 200
        assert response.json()["value"] == TOKEN
        assert response.json()["expires_in"] == sensitive.REVEAL_SECONDS
        assert "no-store" in response["Cache-Control"]

    def test_a_reveal_is_audited(self, client_in, cred, boss):
        client_in.post(reveal_url(cred, "api_token"))
        entry = VantageLogEntry.objects.latest("pk")
        assert entry.actor == boss
        assert entry.revealed_field == "api_token"
        assert str(entry) == "boss revealed api_token on Production key"
        assert not leaks(json.dumps(entry.changes))

    def test_get_is_refused(self, client_in, cred):
        assert client_in.get(reveal_url(cred, "api_token")).status_code == 405

    def test_view_permission_alone_is_not_enough(self, client, cred):
        client.force_login(staff_with("view_zcredential"))
        response = client.post(reveal_url(cred, "api_token"))
        assert response.status_code == 403
        assert TOKEN not in response.content.decode()
        assert not VantageLogEntry.objects.exists()

    def test_the_dedicated_permission_grants_it(self, client, cred):
        client.force_login(staff_with("view_zcredential", "reveal_sensitive"))
        assert client.post(reveal_url(cred, "api_token")).json()["value"] == TOKEN

    def test_the_reveal_permission_without_view_is_nothing(self, client, cred):
        client.force_login(staff_with("reveal_sensitive"))
        assert client.post(reveal_url(cred, "api_token")).status_code == 403

    def test_the_panel_hook_has_the_last_word(self, panel, client_in, cred):
        panel.can_reveal = lambda request, obj, field: field == "card_number"
        assert client_in.post(reveal_url(cred, "api_token")).status_code == 403
        assert client_in.post(reveal_url(cred, "card_number")).json()["value"] == CARD

    def test_a_hash_is_never_revealed(self, panel, client_in, cred):
        panel.can_reveal = lambda request, obj, field: True
        response = client_in.post(reveal_url(cred, "pin_hash"))
        assert response.status_code == 403
        assert not leaks(response.content)

    def test_a_value_in_django_hash_format_is_never_revealed(self, client_in, cred):
        cred.api_token = PIN_HASH
        cred.save()
        response = client_in.post(reveal_url(cred, "api_token"))
        assert response.status_code == 403
        assert not leaks(response.content)

    def test_a_field_that_is_not_sensitive_cannot_be_asked_for(self, client_in, cred):
        assert client_in.post(reveal_url(cred, "name")).status_code == 404

    def test_an_unknown_record_is_a_404(self, client_in, cred):
        url = reverse("vantage:testapp-zcredential-reveal", args=[999999, "api_token"])
        assert client_in.post(url).status_code == 404

    def test_an_out_of_scope_record_is_a_404(self, panel, client_in, cred):
        panel.get_queryset = lambda request: ZCredential.objects.none()
        assert client_in.post(reveal_url(cred, "api_token")).status_code == 404

    def test_anonymous_users_are_sent_to_sign_in(self, client, cred):
        assert client.post(reveal_url(cred, "api_token")).status_code == 302

    @override_settings(VANTAGE={"RATE_LIMITS": {"reveal": (2, 60)}})
    def test_it_is_rate_limited_per_account(self, client_in, cred):
        assert client_in.post(reveal_url(cred, "api_token")).status_code == 200
        assert client_in.post(reveal_url(cred, "card_number")).status_code == 200
        throttled = client_in.post(reveal_url(cred, "pin"))
        assert throttled.status_code == 429
        assert throttled["Retry-After"] == "60"
        assert PIN not in throttled.content.decode()

    def test_the_reveal_control_is_offered_only_to_those_who_may(self, client, client_in, cred):
        assert b"vantage-sensitive.js" in client_in.get(detail_url(cred)).content
        client.force_login(staff_with("view_zcredential"))
        assert b"vantage-sensitive.js" not in client.get(detail_url(cred)).content

    def test_the_permission_is_created_by_migrate(self, db):
        from django.core.management import call_command

        call_command("migrate", verbosity=0)
        assert Permission.objects.filter(codename="reveal_sensitive").exists()


class TestUserPassword:
    @pytest.fixture
    def victim(self, db):
        return User.objects.create_user("victim", "v@example.com", "old-password-123")

    def test_the_hash_is_masked_with_a_set_password_link(self, client_in, victim):
        body = client_in.get(reverse("vantage:auth-user-detail", args=[victim.pk])).content
        text = body.decode()
        assert victim.password not in text and "pbkdf2" not in text
        assert reverse("vantage:auth-user-set-password", args=[victim.pk]) in text
        assert reverse("vantage:auth-user-reveal", args=[victim.pk, "password"]) not in text

    def test_the_hash_is_never_revealed(self, client_in, victim):
        url = reverse("vantage:auth-user-reveal", args=[victim.pk, "password"])
        response = client_in.post(url)
        assert response.status_code == 403
        assert "set-password" in response.json()["set_password_url"]

    def test_the_edit_form_has_no_password_input(self, client_in, victim):
        body = client_in.get(reverse("vantage:auth-user-change", args=[victim.pk])).content
        assert b'name="password"' not in body
        assert victim.password.encode() not in body

    def test_set_password_sets_it_and_audits_without_the_value(self, client_in, victim):
        url = reverse("vantage:auth-user-set-password", args=[victim.pk])
        assert client_in.get(url).status_code == 200
        response = client_in.post(
            url,
            {
                "password1": "a-new-Passw0rd!x",
                "password2": "a-new-Passw0rd!x",
                "usable_password": "true",
            },
        )
        assert response.status_code == 302
        victim.refresh_from_db()
        assert victim.check_password("a-new-Passw0rd!x")
        entry = VantageLogEntry.objects.latest("pk")
        assert entry.changes == {"password": {"redacted": True}}

    def test_setting_your_own_password_keeps_you_signed_in(self, client_in, boss):
        url = reverse("vantage:auth-user-set-password", args=[boss.pk])
        pw = "a-new-Passw0rd!x"
        client_in.post(url, {"password1": pw, "password2": pw, "usable_password": "true"})
        assert client_in.get(detail_url_for_user(boss)).status_code == 200

    def test_set_password_needs_change_permission(self, client, victim):
        client.force_login(staff_with("view_user", model=User))
        url = reverse("vantage:auth-user-set-password", args=[victim.pk])
        assert client.get(url).status_code == 403


class TestSafeStr:
    def test_a_str_without_secrets_is_untouched(self, cred):
        assert sensitive.safe_str(cred) == "Production key"

    def test_a_str_containing_a_secret_is_replaced(self, cred):
        cred.name = f"key {TOKEN}"
        assert sensitive.safe_str(cred) == f"Z credential #{cred.pk}"

    def test_a_short_secret_is_not_looked_for(self, cred):
        cred.pin = "12"
        cred.name = "Order 1234"
        assert sensitive.safe_str(cred) == "Order 1234"
