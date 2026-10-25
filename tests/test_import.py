"""The CSV import wizard: reading a file, mapping it, previewing it, writing it.

The wizard's routes are not mounted by ``tests/urls.py``, so this module builds its own
urlconf with the import routes *before* the panel routes — the detail route's greedy
``<path:pk>`` converter would otherwise swallow ``import/`` as a primary key, which is
the same ordering constraint the real urlconf has.
"""

import csv
import io
import time

import pytest
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import path, reverse

from tests.testapp.models import Author, Category, Post
from vantage import importer, site
from vantage.export import stream_csv
from vantage.importviews import build_import_urls

pytestmark = pytest.mark.urls("tests.test_import")

# Import routes first, for the reason in the module docstring.
urlpatterns = [path("admin/", (build_import_urls(site) + site.get_urls(), "vantage", "vantage"))]

UPLOAD = "vantage:testapp-post-import"
AUTHOR_UPLOAD = "vantage:testapp-author-import"


# ── fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_stash():
    """Uploads land in a real temp directory, so the directory is emptied afterwards."""
    yield
    importer.sweep_stash(now=time.time() + importer.STASH_TTL + 1)


@pytest.fixture
def panel():
    return site.get_panel(Post)


@pytest.fixture
def author_panel():
    return site.get_panel(Author)


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def client_in(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def ada(db):
    return Author.objects.create(name="Ada", email="ada@example.com")


@pytest.fixture
def posts(ada):
    return [
        Post.objects.create(title="Alpha", slug="alpha", body="one", author=ada),
        Post.objects.create(title="Beta", slug="beta", body="two", author=ada),
    ]


def grant(user, model, action):
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(
        Permission.objects.get(content_type=ct, codename=f"{action}_{model._meta.model_name}")
    )
    return User.objects.get(pk=user.pk)  # drop the cached permission set


def as_csv(rows, *, delimiter=",") -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=delimiter).writerows(rows)
    return buffer.getvalue().encode("utf-8")


def plan(panel, raw, *, key_field=None, mode="create", chosen=None):
    """Parse and resolve in one step, the way a view does."""
    parsed = importer.read_csv(raw)
    options = importer.importable_fields(panel)
    mapping = importer.resolve_mapping(
        panel,
        parsed,
        chosen if chosen is not None else importer.infer_mapping(parsed.headers, options),
        key_field=key_field,
        mode=mode,
    )
    return parsed, mapping


def upload(client, raw, *, url=UPLOAD, name="posts.csv"):
    return client.post(
        reverse(url), {"file": SimpleUploadedFile(name, raw, content_type="text/csv")}
    )


def wizard(client, raw, *, url=UPLOAD, mode=None, key=None, overrides=None, commit=False):
    """Drive the whole wizard the way a browser does, and hand back the last response."""
    started = upload(client, raw, url=url)
    assert started.status_code == 302, started.status_code
    mapped = client.get(started["Location"])
    data = {f"map-{column['index']}": column["target"] for column in mapped.context["columns"]}
    data["key_field"] = mapped.context["default_key"] if key is None else key
    data["mode"] = mapped.context["default_mode"] if mode is None else mode
    data.update(overrides or {})

    previewed = client.post(mapped.context["preview_url"], data)
    if not commit:
        return previewed
    hidden = {field["name"]: field["value"] for field in previewed.context["hidden"]}
    return client.post(previewed.context["commit_url"], hidden)


# ── step 1: reading the file ────────────────────────────────────────────────


class TestReadCsv:
    def test_reads_headers_and_rows(self):
        parsed = importer.read_csv(as_csv([["Title", "Slug"], ["Alpha", "alpha"]]))
        assert parsed.headers == ("Title", "Slug")
        assert parsed.rows == (("Alpha", "alpha"),)

    def test_sniffs_a_semicolon_delimiter(self):
        raw = as_csv([["Title", "Slug"], ["Alpha", "alpha"]], delimiter=";")
        parsed = importer.read_csv(raw)
        assert parsed.delimiter == ";"
        assert parsed.headers == ("Title", "Slug")

    def test_a_byte_order_mark_does_not_stick_to_the_first_header(self):
        """The bug this ordering exists to prevent: utf-8 decodes a BOM file happily."""
        raw = "Title,Slug\r\nAlpha,alpha\r\n".encode("utf-8-sig")
        parsed = importer.read_csv(raw)
        assert parsed.headers[0] == "Title"
        assert parsed.encoding == "utf-8-sig"

    def test_falls_back_to_latin_1(self):
        raw = "Title\nCaf\xe9".encode("latin-1")
        parsed = importer.read_csv(raw)
        assert parsed.rows == (("Café",),)
        assert parsed.encoding == "latin-1"

    def test_ignores_a_trailing_blank_line(self):
        parsed = importer.read_csv(b"Title\nAlpha\n\n")
        assert parsed.row_count == 1

    def test_an_empty_file_is_refused(self):
        with pytest.raises(importer.ImportRejected, match="empty"):
            importer.read_csv(b"")

    def test_a_header_with_no_rows_is_refused(self):
        with pytest.raises(importer.ImportRejected, match="no data rows"):
            importer.read_csv(b"Title,Slug\n")

    def test_a_binary_file_is_refused_rather_than_mangled(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(32)) * 4
        with pytest.raises(importer.ImportRejected, match="text file"):
            importer.read_csv(png)

    def test_a_numeric_first_row_is_not_plausible_headers(self):
        with pytest.raises(importer.ImportRejected, match="Add a header row"):
            importer.read_csv(b"1,2,3\n4,5,6\n")

    def test_a_prose_first_row_is_not_plausible_headers(self):
        raw = as_csv([["x" * 200, "b"], ["1", "2"]])
        with pytest.raises(importer.ImportRejected, match="looks like data"):
            importer.read_csv(raw)

    def test_a_blank_column_name_is_refused(self):
        with pytest.raises(importer.ImportRejected, match="column 2 has no name"):
            importer.read_csv(b"Title,,Slug\nA,B,C\n")

    def test_duplicate_column_names_are_refused(self):
        with pytest.raises(importer.ImportRejected, match="Two columns"):
            importer.read_csv(b"Title,title\nA,B\n")

    def test_the_row_cap_is_enforced(self):
        raw = as_csv([["Title"], *[[f"P{n}"] for n in range(5)]])
        with pytest.raises(importer.ImportRejected, match="import limit"):
            importer.read_csv(raw, max_rows=3)

    def test_the_size_cap_is_enforced(self):
        with pytest.raises(importer.ImportRejected, match="the limit is"):
            importer.read_csv(as_csv([["Title"], ["A"]]), max_bytes=4)


# ── step 2: which field does this column feed ───────────────────────────────


class TestImportableFields:
    def test_offers_the_primary_key_as_an_identifier_only(self, panel):
        options = {option.name: option for option in importer.importable_fields(panel)}
        assert options["id"].writable is False
        assert options["id"].unique is True

    def test_drops_many_to_many_and_files(self, panel, author_panel):
        assert "tags" not in {option.name for option in importer.importable_fields(panel)}
        assert "avatar" not in {option.name for option in importer.importable_fields(author_panel)}

    def test_drops_readonly_fields(self, panel, monkeypatch):
        monkeypatch.setattr(panel, "readonly_fields", ["status"])
        assert "status" not in {option.name for option in importer.importable_fields(panel)}

    def test_key_options_are_the_unique_ones(self, panel):
        names = {option.name for option in importer.key_options(importer.importable_fields(panel))}
        assert names == {"id", "slug"}


class TestMappingInference:
    def test_matches_an_exact_field_name(self, panel):
        options = importer.importable_fields(panel)
        assert importer.infer_mapping(["title"], options) == {"title": "title"}

    def test_ignores_case_spaces_and_underscores(self, panel):
        options = importer.importable_fields(panel)
        assert importer.infer_mapping(["View Count"], options) == {"View Count": "view_count"}

    def test_matches_a_verbose_name(self):
        options = [
            importer.FieldOption(
                name="tier_code", label="Membership level", kind="text", writable=True, unique=False
            )
        ]
        assert importer.infer_mapping(["Membership Level"], options) == {
            "Membership Level": "tier_code"
        }

    def test_an_exact_match_wins_over_an_earlier_fuzzy_one(self):
        """Every pass runs over every header before the next begins, for this reason."""
        options = [
            importer.FieldOption(
                name="name", label="Title", kind="text", writable=True, unique=False
            ),
            importer.FieldOption(
                name="title", label="Heading", kind="text", writable=True, unique=False
            ),
        ]
        assert importer.infer_mapping(["Title", "name"], options) == {
            "Title": "title",
            "name": "name",
        }

    def test_a_field_is_claimed_once(self, panel):
        options = importer.importable_fields(panel)
        assert importer.infer_mapping(["title", "Title"], options) == {"title": "title"}

    def test_an_unrecognised_column_is_left_unmapped(self, panel):
        options = importer.importable_fields(panel)
        assert importer.infer_mapping(["Nonsense"], options) == {}


class TestResolveMapping:
    """The security boundary: nothing downstream sees an unvalidated field name."""

    def test_an_unknown_target_is_ignored_not_obeyed(self, panel):
        parsed = importer.read_csv(as_csv([["Title", "Whatever"], ["A", "B"]]))
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            {"Title": "title", "Whatever": "not_a_field"},
            key_field=None,
            mode="create",
        )
        assert mapping.columns == {"Title": "title"}
        assert ("Whatever", "read-only — nothing is written to it") in mapping.ignored

    def test_a_readonly_target_is_ignored_even_when_posted_by_hand(self, panel, monkeypatch):
        monkeypatch.setattr(panel, "readonly_fields", ["status"])
        parsed = importer.read_csv(as_csv([["Title", "Status"], ["A", "draft"]]))
        mapping = importer.resolve_mapping(
            panel, parsed, {"Title": "title", "Status": "status"}, key_field=None, mode="create"
        )
        assert "status" not in mapping.targets

    def test_two_columns_cannot_feed_one_field(self, panel):
        parsed = importer.read_csv(as_csv([["A", "B"], ["1", "2"]]))
        mapping = importer.resolve_mapping(
            panel, parsed, {"A": "title", "B": "title"}, key_field=None, mode="create"
        )
        assert mapping.columns == {"A": "title"}
        assert mapping.ignored == (("B", "“A” already feeds that field"),)

    def test_a_required_field_left_unmapped_is_flagged_missing(self, panel):
        """The general case the tenancy exemption in ``_missing_required`` must not
        weaken: ``Post.author`` has no default and is not nullable, so a create without
        it would fail a NOT NULL constraint one row at a time. This project has no
        ``TENANT_MODEL`` configured, so ``vantage.tenancy.scope_for(Post)`` reports it
        as unscoped (``GLOBAL``) and exempts nothing - the guard must still catch this
        exactly as it always has."""
        parsed = importer.read_csv(as_csv([["Title", "Slug", "Body"], ["A", "a", "x"]]))
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            {"Title": "title", "Slug": "slug", "Body": "body"},
            key_field=None,
            mode="create",
        )
        assert ("author", "Author") in mapping.missing_required

    def test_a_non_unique_key_is_refused(self, panel):
        parsed = importer.read_csv(as_csv([["Title"], ["A"]]))
        with pytest.raises(importer.ImportRejected, match="unique field"):
            importer.resolve_mapping(
                panel, parsed, {"Title": "title"}, key_field="title", mode="upsert"
            )

    def test_updating_without_a_key_is_refused(self, panel):
        parsed = importer.read_csv(as_csv([["Title"], ["A"]]))
        with pytest.raises(importer.ImportRejected, match="identifies an existing record"):
            importer.resolve_mapping(
                panel, parsed, {"Title": "title"}, key_field=None, mode="update"
            )

    def test_a_key_that_no_column_feeds_is_refused(self, panel):
        parsed = importer.read_csv(as_csv([["Title"], ["A"]]))
        with pytest.raises(importer.ImportRejected, match="No column is mapped"):
            importer.resolve_mapping(
                panel, parsed, {"Title": "title"}, key_field="slug", mode="upsert"
            )

    def test_a_mapping_that_writes_nothing_is_refused(self, panel):
        parsed = importer.read_csv(as_csv([["Id"], ["1"]]))
        with pytest.raises(importer.ImportRejected, match="do nothing"):
            importer.resolve_mapping(panel, parsed, {"Id": "id"}, key_field="id", mode="upsert")

    def test_an_unknown_mode_is_refused(self, panel):
        parsed = importer.read_csv(as_csv([["Title"], ["A"]]))
        with pytest.raises(importer.ImportRejected):
            importer.resolve_mapping(
                panel, parsed, {"Title": "title"}, key_field=None, mode="delete"
            )


# ── step 3: the dry run ─────────────────────────────────────────────────────

ROWS = [
    ["Title", "Slug", "Body", "Author"],
    ["Gamma", "gamma", "three", "Ada"],
    ["Delta", "delta", "four", "Ada"],
]


class TestDryRun:
    def test_creates_nothing(self, panel, ada):
        parsed, mapping = plan(panel, as_csv(ROWS))
        result = importer.dry_run(panel, parsed, mapping)
        assert result.created == 2
        assert Post.objects.count() == 0

    def test_leaves_an_updated_row_untouched(self, panel, posts):
        raw = as_csv([["Slug", "Title"], ["alpha", "Renamed"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        result = importer.dry_run(panel, parsed, mapping)
        assert result.updated == 1
        assert Post.objects.get(slug="alpha").title == "Alpha"

    def test_reports_a_validation_error_with_its_row_and_column(self, panel, ada):
        raw = as_csv(
            [
                ["Title", "Slug", "Body", "Status", "Author"],
                ["Gamma", "gamma", "x", "banana", "Ada"],
            ]
        )
        parsed, mapping = plan(panel, raw)
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert result.created == 0
        error = result.errors[0]
        # Row 2 because the header is row 1 — the numbering the user sees in the file.
        assert error.row == 2
        assert error.column == "Status"
        assert "valid choice" in error.message

    def test_reports_a_unique_collision_between_the_file_and_the_table(self, panel, posts):
        raw = as_csv([["Title", "Slug", "Body", "Author"], ["Gamma", "alpha", "x", "Ada"]])
        parsed, mapping = plan(panel, raw)
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert result.errors[0].column == "Slug"

    def test_a_row_of_the_wrong_width_fails_rather_than_shifting_values(self, panel, ada):
        raw = b"Title,Slug,Body,Author\nGamma,gamma\n"
        parsed, mapping = plan(panel, raw)
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert "2 values but the header has 4 columns" in result.errors[0].message

    def test_counts_an_identical_row_as_unchanged(self, panel, posts):
        raw = as_csv([["Slug", "Title"], ["alpha", "Alpha"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        result = importer.dry_run(panel, parsed, mapping)
        assert (result.unchanged, result.updated) == (1, 0)

    def test_shows_a_before_and_after_diff(self, panel, posts):
        raw = as_csv([["Slug", "Title"], ["alpha", "Renamed"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        diff = importer.dry_run(panel, parsed, mapping).diffs[0]
        assert diff.label == "Alpha"
        assert diff.changes == (importer.RowChange(label="Title", before="Alpha", after="Renamed"),)

    def test_a_relation_diff_reads_as_labels_not_ids(self, panel, posts):
        grace = Author.objects.create(name="Grace", email="grace@example.com")
        raw = as_csv([["Slug", "Author"], ["alpha", str(grace.pk)]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        change = importer.dry_run(panel, parsed, mapping).diffs[0].changes[0]
        assert (change.before, change.after) == ("Ada", "Grace")

    def test_summary_names_every_outcome(self):
        result = importer.ImportResult(dry_run=True, created=1204, failed=3)
        assert result.summary() == "1,204 created, 3 failed"


class TestModes:
    def test_create_only_refuses_an_existing_key(self, panel, posts):
        raw = as_csv([["Slug", "Title", "Body"], ["alpha", "Again", "x"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="create")
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert "already exists" in result.errors[0].message
        assert result.errors[0].column == "Slug"

    def test_update_only_refuses_a_missing_key(self, panel, ada):
        raw = as_csv([["Slug", "Title"], ["nope", "Ghost"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert "No existing record matches" in result.errors[0].message

    def test_upsert_updates_rather_than_duplicating(self, panel, posts, ada):
        raw = as_csv(
            [
                ["Slug", "Title", "Body", "Author"],
                ["alpha", "Renamed", "one", "Ada"],
                ["new", "Fresh", "x", "Ada"],
            ]
        )
        parsed, mapping = plan(panel, raw, key_field="slug", mode="upsert")
        result = importer.commit(panel, parsed, mapping)
        assert (result.created, result.updated) == (1, 1)
        assert Post.objects.filter(slug="alpha").count() == 1
        assert Post.objects.get(slug="alpha").title == "Renamed"

    def test_an_empty_key_cell_is_an_error_not_a_blind_create(self, panel, posts):
        raw = as_csv([["Slug", "Title"], ["", "Nameless"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="upsert")
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert "cannot be empty" in result.errors[0].message


class TestCoercion:
    def test_a_foreign_key_may_be_given_as_a_label(self, panel, ada):
        parsed, mapping = plan(panel, as_csv(ROWS))
        importer.commit(panel, parsed, mapping)
        assert Post.objects.get(slug="gamma").author == ada

    def test_an_ambiguous_label_is_an_error_not_a_coin_flip(self, panel, ada):
        Author.objects.create(name="Ada", email="ada2@example.com")
        parsed, mapping = plan(panel, as_csv(ROWS))
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 2
        assert "more than one author" in result.errors[0].message

    def test_one_pass_reports_everything_wrong_with_a_row(self, panel, ada):
        """Fixing a file one error per attempt is the loop this avoids."""
        raw = as_csv(
            [
                ["Title", "Slug", "Body", "Author", "Status"],
                ["G", "g", "x", "Nobody", "banana"],
            ]
        )
        parsed, mapping = plan(panel, raw)
        result = importer.dry_run(panel, parsed, mapping)
        assert result.failed == 1
        assert {error.column for error in result.errors} == {"Author", "Status"}

    def test_an_unmatched_label_names_the_column(self, panel, ada):
        raw = as_csv([["Title", "Slug", "Body", "Author"], ["G", "g", "x", "Nobody"]])
        parsed, mapping = plan(panel, raw)
        error = importer.dry_run(panel, parsed, mapping).errors[0]
        assert error.column == "Author"
        assert "No author matches" in error.message

    def test_zero_is_false(self, author_panel, db):
        """``CheckboxInput`` reads "0" as True, because it is a non-empty string."""
        raw = as_csv([["Name", "Email", "Is active"], ["Zoe", "z@example.com", "0"]])
        parsed, mapping = plan(author_panel, raw)
        importer.commit(author_panel, parsed, mapping)
        assert Author.objects.get(name="Zoe").is_active is False

    def test_true_is_true(self, author_panel, db):
        raw = as_csv([["Name", "Email", "Is active"], ["Zoe", "z@example.com", "true"]])
        parsed, mapping = plan(author_panel, raw)
        importer.commit(author_panel, parsed, mapping)
        assert Author.objects.get(name="Zoe").is_active is True


class TestReadonlyColumns:
    def test_a_readonly_column_is_never_written(self, panel, posts, monkeypatch):
        monkeypatch.setattr(panel, "readonly_fields", ["title"])
        raw = as_csv([["Slug", "Title", "Body", "Author"], ["alpha", "Hijacked", "one", "Ada"]])
        parsed, mapping = plan(panel, raw, key_field="slug", mode="update")
        result = importer.commit(panel, parsed, mapping)
        assert Post.objects.get(slug="alpha").title == "Alpha"
        assert result.unchanged == 1

    def test_an_auto_timestamp_column_is_ignored(self, author_panel, db):
        """The exported "Created at" column has nowhere to go, and must not error."""
        raw = as_csv(
            [["Name", "Email", "Created at"], ["Zoe", "z@example.com", "2020-01-01 00:00:00+00:00"]]
        )
        parsed, mapping = plan(author_panel, raw)
        assert ("Created at", "skipped") in mapping.ignored
        assert importer.dry_run(author_panel, parsed, mapping).created == 1

    def test_an_auto_timestamp_posted_by_hand_is_still_ignored(self, author_panel, db):
        raw = as_csv([["Name", "Email", "Created at"], ["Zoe", "z@example.com", "2020-01-01"]])
        parsed, mapping = plan(
            author_panel,
            raw,
            chosen={"Name": "name", "Email": "email", "Created at": "created_at"},
        )
        assert ("Created at", "read-only — nothing is written to it") in mapping.ignored
        importer.commit(author_panel, parsed, mapping)
        assert Author.objects.get(name="Zoe").created_at.year != 2020


# ── step 4: the commit ──────────────────────────────────────────────────────


class TestCommit:
    def test_writes_the_rows(self, panel, ada):
        parsed, mapping = plan(panel, as_csv(ROWS))
        result = importer.commit(panel, parsed, mapping)
        assert result.created == 2
        assert Post.objects.count() == 2

    def test_reports_partial_failure_honestly(self, panel, ada):
        rows = [
            ["Title", "Slug", "Body", "Status", "Author"],
            ["One", "one", "x", "draft", "Ada"],
            ["Two", "two", "x", "banana", "Ada"],
            ["Three", "three", "x", "draft", "Ada"],
        ]
        parsed, mapping = plan(panel, as_csv(rows))
        result = importer.commit(panel, parsed, mapping)
        assert (result.created, result.failed) == (2, 1)
        assert result.summary() == "2 created, 1 failed"
        assert Post.objects.count() == 2

    def test_good_rows_in_an_earlier_chunk_survive_a_later_failure(self, panel, ada):
        rows = [["Title", "Slug", "Body", "Status", "Author"]]
        rows += [[f"P{n}", f"p{n}", "x", "draft", "Ada"] for n in range(4)]
        rows[3][3] = "banana"
        parsed, mapping = plan(panel, as_csv(rows))
        result = importer.commit(panel, parsed, mapping, chunk_size=1)
        assert (result.created, result.failed) == (3, 1)
        assert set(Post.objects.values_list("slug", flat=True)) == {"p0", "p1", "p3"}

    def test_a_required_field_left_unmapped_without_tenancy_fails_every_row(self, panel, db):
        """No tenancy is configured in this settings module, so there is nothing for
        ``vantage.tenancy.scope_for`` to exempt: a genuinely required, unmapped column
        must still refuse the row rather than let it half-create. This is the guard the
        tenancy exemption must narrow around, never weaken."""
        rows = [["Title", "Slug", "Body"], ["A", "a", "x"]]
        parsed, mapping = plan(
            panel, as_csv(rows), chosen={"Title": "title", "Slug": "slug", "Body": "body"}
        )
        assert mapping.missing_required
        result = importer.commit(panel, parsed, mapping)
        assert result.created == 0
        assert result.failed == 1
        assert "must have a value" in result.errors[0].message
        assert not Post.objects.filter(slug="a").exists()

    def test_a_database_error_rolls_back_only_its_own_row(self, panel, ada, monkeypatch):
        """The savepoint per row is what makes "1,204 created, 3 failed" possible."""
        original = Post.save

        def explode(self, *args, **kwargs):
            if self.slug == "delta":
                raise IntegrityError("contrived constraint failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Post, "save", explode)
        parsed, mapping = plan(panel, as_csv(ROWS))
        result = importer.commit(panel, parsed, mapping)
        assert (result.created, result.failed) == (1, 1)
        assert "contrived constraint failure" in result.errors[0].message
        assert list(Post.objects.values_list("slug", flat=True)) == ["gamma"]


# ── the error report ────────────────────────────────────────────────────────


class TestErrorReport:
    def test_carries_the_row_the_column_and_the_message(self, panel, ada):
        raw = as_csv(
            [["Title", "Slug", "Body", "Status", "Author"], ["G", "g", "x", "banana", "Ada"]]
        )
        parsed, mapping = plan(panel, raw)
        rows = importer.error_csv_rows(importer.dry_run(panel, parsed, mapping))
        assert rows[0] == ["Row", "Column", "Error"]
        assert rows[1][0] == "2"
        assert rows[1][1] == "Status"

    def test_a_formula_cell_is_neutralised(self, panel, ada):
        """A column called ``=cmd|…`` arrives in the header and leaves in the report."""
        raw = as_csv(
            [["Title", "Body", "Author", "=cmd|'/c calc'!A1"], ["G", "x", "Ada", "banana"]]
        )
        parsed = importer.read_csv(raw)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            {"Title": "title", "Body": "body", "Author": "author", "=cmd|'/c calc'!A1": "status"},
            key_field=None,
            mode="create",
        )
        result = importer.dry_run(panel, parsed, mapping)
        # The invalid status is what puts the dangerous column name in the report.
        assert result.failed == 1
        written = importer.error_csv_bytes(result).decode()
        assert "'=cmd" in written
        assert ",=cmd" not in written

    @pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
    def test_every_dangerous_prefix_is_armoured(self, value):
        assert importer.armour(value).startswith("'")

    def test_an_ordinary_cell_is_left_alone(self):
        assert importer.armour("Status") == "Status"


# ── the round trip ──────────────────────────────────────────────────────────


class TestRoundTrip:
    """Export's columns are import's columns. If that stops being true, this fails."""

    COLUMNS = ["id", "title", "status", "featured", "author", "category", "view_count"]
    HEADERS = ["Id", "Title", "Status", "Featured", "Author", "Category", "View count"]

    def exported(self, queryset) -> bytes:
        response = stream_csv(queryset, self.COLUMNS, self.HEADERS, "posts")
        return b"".join(response.streaming_content)

    def test_re_importing_an_export_changes_nothing(self, panel, posts):
        Post.objects.filter(slug="beta").update(
            category=Category.objects.create(name="News", slug="news"),
            status="published",
            featured=True,
            view_count=17,
        )
        parsed, mapping = plan(
            panel, self.exported(Post.objects.all()), key_field="id", mode="upsert"
        )
        result = importer.dry_run(panel, parsed, mapping)
        assert result.errors == []
        assert (result.created, result.updated, result.unchanged) == (0, 0, 2)

    def test_every_exported_column_is_recognised(self, panel, posts):
        parsed = importer.read_csv(self.exported(Post.objects.all()))
        mapping = importer.infer_mapping(parsed.headers, importer.importable_fields(panel))
        assert set(mapping) == set(self.HEADERS)

    def test_the_list_pages_own_export_maps_cleanly(self, panel, posts):
        from vantage.views import ListView

        columns = panel.get_list_display(None)
        headers = [ListView._column_header(panel, name, None)["label"] for name in columns]
        mapping = importer.infer_mapping(headers, importer.importable_fields(panel))
        assert set(mapping) == set(headers)


# ── the wizard ──────────────────────────────────────────────────────────────


class TestUploadStep:
    def test_shows_the_columns_it_understands(self, client_in):
        page = client_in.get(reverse(UPLOAD))
        assert page.status_code == 200
        assert "title" in {option.name for option in page.context["options"]}

    def test_a_good_file_redirects_to_the_mapping_step(self, client_in, ada):
        response = upload(client_in, as_csv(ROWS))
        assert response.status_code == 302
        assert "/import/" in response["Location"]

    def test_a_missing_file_is_refused_cleanly(self, client_in):
        response = client_in.post(reverse(UPLOAD), {})
        assert response.status_code == 400
        assert "Choose a CSV file" in response.context["error"]

    def test_an_empty_file_is_refused_cleanly(self, client_in):
        response = upload(client_in, b"")
        assert response.status_code == 400
        assert "empty" in response.context["error"]

    def test_a_binary_file_is_refused_cleanly(self, client_in):
        response = upload(client_in, b"\x89PNG\r\n\x1a\n\x00\x00" + bytes(range(32)) * 8)
        assert response.status_code == 400
        assert "text file" in response.context["error"]

    def test_a_headerless_file_is_refused_cleanly(self, client_in):
        response = upload(client_in, b"1,2,3\n4,5,6\n")
        assert response.status_code == 400
        assert "header row" in response.context["error"]


class TestMapStep:
    def test_infers_a_mapping_and_a_key(self, client_in, posts):
        started = upload(client_in, as_csv(ROWS))
        page = client_in.get(started["Location"])
        assert {column["header"]: column["target"] for column in page.context["columns"]} == {
            "Title": "title",
            "Slug": "slug",
            "Body": "body",
            "Author": "author",
        }
        assert page.context["default_key"] == "slug"
        assert page.context["default_mode"] == "create"

    def test_a_file_with_ids_defaults_to_upsert(self, client_in, posts):
        raw = as_csv([["Id", "Title"], [str(posts[0].pk), "Renamed"]])
        started = upload(client_in, raw)
        page = client_in.get(started["Location"])
        assert page.context["default_key"] == "id"
        assert page.context["default_mode"] == "upsert"

    def test_an_expired_token_sends_you_back_to_the_start(self, client_in):
        url = reverse("vantage:testapp-post-import-map", args=["x" * 30])
        response = client_in.get(url)
        assert response.status_code == 302
        assert response["Location"] == reverse(UPLOAD)

    def test_an_unusable_mapping_returns_to_this_step_with_the_reason(self, client_in, ada):
        response = wizard(client_in, as_csv(ROWS), mode="update", key="")
        assert response.status_code == 400
        assert "identifies an existing record" in response.context["error"]


class TestPreviewStep:
    def test_previews_without_writing(self, client_in, ada):
        response = wizard(client_in, as_csv(ROWS))
        assert response.status_code == 200
        assert response.context["result"].created == 2
        assert Post.objects.count() == 0

    def test_offers_the_error_report(self, client_in, ada):
        raw = as_csv(
            [["Title", "Slug", "Body", "Status", "Author"], ["G", "g", "x", "banana", "Ada"]]
        )
        response = wizard(client_in, raw)
        report = client_in.get(response.context["errors_url"])
        assert report.status_code == 200
        assert report["Cache-Control"] == "no-store"
        assert "attachment" in report["Content-Disposition"]
        assert b"Status" in report.content


class TestCommitStep:
    def test_writes_and_redirects_to_the_list(self, client_in, ada):
        response = wizard(client_in, as_csv(ROWS), commit=True)
        assert response.status_code == 302
        assert Post.objects.count() == 2

    def test_the_upload_is_deleted_once_it_has_been_committed(self, client_in, ada):
        started = upload(client_in, as_csv(ROWS))
        token = started["Location"].rstrip("/").rsplit("/", 1)[-1]
        mapped = client_in.get(started["Location"])
        data = {f"map-{c['index']}": c["target"] for c in mapped.context["columns"]}
        data.update(key_field=mapped.context["default_key"], mode=mapped.context["default_mode"])
        previewed = client_in.post(mapped.context["preview_url"], data)
        hidden = {field["name"]: field["value"] for field in previewed.context["hidden"]}
        assert client_in.post(previewed.context["commit_url"], hidden).status_code == 302
        with pytest.raises(importer.StashExpired):
            importer.read_stash(
                user=User.objects.get(username="boss"), model_label="testapp.post", token=token
            )

    def test_an_id_keyed_upsert_survives_the_hidden_mapping(self, client_in, posts):
        """The key column is not writable, so the commit form has to carry it separately."""
        raw = as_csv([["Id", "Title"], [str(posts[0].pk), "Renamed"]])
        response = wizard(client_in, raw, commit=True)
        assert response.status_code == 302
        assert Post.objects.get(pk=posts[0].pk).title == "Renamed"
        assert Post.objects.count() == 2

    def test_failures_stay_on_the_page_with_the_report(self, client_in, ada):
        rows = [
            ["Title", "Slug", "Body", "Status", "Author"],
            ["One", "one", "x", "draft", "Ada"],
            ["Two", "two", "x", "banana", "Ada"],
        ]
        response = wizard(client_in, as_csv(rows), commit=True)
        assert response.status_code == 200
        assert response.context["done"] is True
        assert (response.context["result"].created, response.context["result"].failed) == (1, 1)
        assert Post.objects.count() == 1


# ── access ──────────────────────────────────────────────────────────────────


class TestAccess:
    def test_anonymous_is_sent_to_the_login_page(self, client, db):
        response = client.get(reverse(UPLOAD))
        assert response.status_code == 302

    def test_a_user_without_add_is_refused(self, client, db):
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        staff = grant(staff, Post, "view")
        client.force_login(staff)
        assert client.get(reverse(UPLOAD)).status_code == 403

    def test_add_alone_allows_creating(self, client, ada):
        staff = grant(User.objects.create_user("adder", password="x", is_staff=True), Post, "add")
        client.force_login(staff)
        response = wizard(client, as_csv(ROWS), commit=True)
        assert response.status_code == 302
        assert Post.objects.count() == 2

    def test_add_alone_does_not_allow_upserting(self, client, posts):
        staff = grant(User.objects.create_user("adder", password="x", is_staff=True), Post, "add")
        client.force_login(staff)
        raw = as_csv([["Slug", "Title", "Body", "Author"], ["alpha", "Renamed", "one", "Ada"]])
        response = wizard(client, raw, mode="upsert", key="slug")
        assert response.status_code == 403
        assert Post.objects.get(slug="alpha").title == "Alpha"

    def test_only_permitted_modes_are_offered(self, client, ada):
        staff = grant(User.objects.create_user("adder", password="x", is_staff=True), Post, "add")
        client.force_login(staff)
        started = upload(client, as_csv(ROWS))
        page = client.get(started["Location"])
        assert [mode["value"] for mode in page.context["modes"]] == ["create"]

    def test_one_users_upload_is_not_another_users_to_read(self, client_in, client, ada):
        started = upload(client_in, as_csv(ROWS))
        other = grant(User.objects.create_user("other", password="x", is_staff=True), Post, "add")
        client.force_login(other)
        response = client.get(started["Location"])
        assert response.status_code == 302
        assert response["Location"] == reverse(UPLOAD)

    def test_an_uploads_token_is_bound_to_its_model(self, client_in, ada):
        started = upload(client_in, as_csv(ROWS))
        token = started["Location"].rstrip("/").rsplit("/", 1)[-1]
        elsewhere = reverse("vantage:testapp-author-import-map", args=[token])
        response = client_in.get(elsewhere)
        assert response.status_code == 302
        assert response["Location"] == reverse(AUTHOR_UPLOAD)


class TestStash:
    def test_a_traversal_token_reads_nothing(self, boss):
        with pytest.raises(importer.StashExpired):
            importer.read_stash(user=boss, model_label="testapp.post", token="../../etc/passwd")

    def test_discarding_is_idempotent(self):
        importer.discard_stash("nonexistent-token-that-is-long-enough")

    def test_an_expired_upload_is_swept(self, boss):
        token = importer.stash_upload(
            user=boss, model_label="testapp.post", filename="a.csv", raw=b"Title\nA\n"
        )
        assert importer.sweep_stash(now=time.time() + importer.STASH_TTL + 1) >= 1
        with pytest.raises(importer.StashExpired):
            importer.read_stash(user=boss, model_label="testapp.post", token=token)
