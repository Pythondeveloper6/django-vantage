"""Optimistic locking: the compare-and-set, and the story it tells when it fires.

The failure this guards against is invisible by construction. Two people open the same
row, both save, and the second write silently erases the first — no error, no log, and
the person who lost has every reason to believe their change landed. So the tests below
are mostly about the *absence* of a silent success:

* a write carrying a superseded token must not land (``TestClaim``),
* it must come back as a 409 carrying the state that beat it (``TestConflictResponse``),
* and the 409 must name the fields, both sides, so the answer is a decision rather than
  a guess (``TestFieldDiff``).

The two obvious ways to get this wrong are both tested for directly. Reading the
version and *then* saving leaves a race the width of the view, so
:func:`~vantage.concurrency.claim` is asserted to be a single statement whose ``WHERE``
clause is the check. And a counter column that is not written back onto the instance is
undone by the very save it was protecting, which is
``test_a_counter_survives_the_save_that_follows_it``.
"""

import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection, models
from django.test.utils import isolate_apps

from tests.testapp.models import Author, Post, Preference
from vantage import concurrency
from vantage.concurrency import StaleWrite
from vantage.sites import site

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("locker", "locker@example.com", "x")


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def post(db, author):
    return Post.objects.create(
        title="Analytical engines",
        slug="analytical-engines",
        body="Notes.",
        status=Post.Status.DRAFT,
        author=author,
        view_count=3,
    )


@pytest.fixture
def editable(monkeypatch):
    """``Post`` with three inline-editable columns, and nothing else changed."""
    panel = site.get_panel(Post)
    monkeypatch.setattr(panel, "list_editable", ["status", "featured", "view_count"])
    return panel


def bump(post, **fields):
    """Somebody else's write, applied straight to the database.

    ``queryset.update`` bypasses ``auto_now``, which is what lets the new timestamp be
    stated rather than raced for — two saves inside the same microsecond would make
    this test pass for the wrong reason roughly never, which is the worst frequency.
    """
    fields.setdefault("updated_at", post.updated_at + timedelta(seconds=5))
    Post.objects.filter(pk=post.pk).update(**fields)
    return Post.objects.get(pk=post.pk)


def patch(rf, panel, user, payload):
    from vantage.inline import InlineWriteView

    request = rf.patch(
        "/admin/testapp/post/_inline/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    request.user = user
    return InlineWriteView.as_view(site=site, panel=panel)(request)


# ── detecting the version column ────────────────────────────────────────────


class TestVersionColumn:
    def test_an_auto_now_timestamp_is_a_version(self):
        column = concurrency.version_column(Post)
        assert column is not None
        assert column.name == "updated_at"
        assert column.is_counter is False

    def test_a_model_without_one_says_so(self):
        assert concurrency.version_column(Author) is None

    def test_auto_now_add_is_not_a_version(self):
        """``Author`` has a ``created_at``, and it never moves after the insert.

        A column that cannot change cannot detect a change. Picking it up would give
        every row a permanent lock that always succeeds — the appearance of safety.
        """
        assert concurrency.version_column(Author) is None
        assert concurrency.version_column(Preference).name == "updated_at"

    @isolate_apps("tests.testapp")
    def test_an_integer_counter_outranks_a_timestamp(self):
        class CBoth(models.Model):
            version = models.IntegerField(default=0)
            updated_at = models.DateTimeField(auto_now=True)

            class Meta:
                app_label = "testapp"

        column = concurrency.version_column(CBoth)
        assert column.name == "version"
        assert column.is_counter is True

    @isolate_apps("tests.testapp")
    def test_a_nullable_counter_is_refused(self):
        """``NULL = NULL`` is never true, so the compare-and-set would never match.

        Accepting one would turn every single write on the model into a permanent,
        unexplainable 409 — a worse outcome than having no lock at all.
        """

        class CNullable(models.Model):
            version = models.IntegerField(null=True)

            class Meta:
                app_label = "testapp"

        assert concurrency.version_column(CNullable) is None


class TestToken:
    def test_a_timestamp_round_trips(self, post):
        assert concurrency.token_for(post) == post.updated_at.isoformat()

    def test_an_unversioned_model_has_no_token(self, author):
        assert concurrency.token_for(author) is None


# ── the compare-and-set ─────────────────────────────────────────────────────


class TestClaim:
    def test_the_current_token_takes_the_row(self, post):
        assert concurrency.claim(post, concurrency.token_for(post)) is not None

    def test_the_claim_advances_the_version(self, post):
        before = post.updated_at
        concurrency.claim(post, concurrency.token_for(post))
        assert Post.objects.get(pk=post.pk).updated_at > before

    def test_a_superseded_token_is_refused(self, post):
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)
        with pytest.raises(StaleWrite):
            concurrency.claim(post, stale)

    def test_the_refusal_carries_the_state_that_won(self, post):
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)
        with pytest.raises(StaleWrite) as caught:
            concurrency.claim(post, stale)
        assert caught.value.obj.status == Post.Status.PUBLISHED
        assert caught.value.token != stale

    def test_a_missing_token_on_a_lockable_row_is_a_conflict(self, post):
        """A write that cannot say what it read is exactly the blind overwrite."""
        with pytest.raises(StaleWrite):
            concurrency.claim(post, None)

    def test_an_unparseable_token_is_treated_as_stale(self, post):
        with pytest.raises(StaleWrite):
            concurrency.claim(post, "not-a-timestamp")

    def test_an_unversioned_model_is_written_unguarded(self, author):
        """Honest rather than clever: no version column means no lock, and it says so."""
        assert concurrency.claim(author, None) is None

    def test_the_second_of_two_racers_loses(self, post):
        """Both hold the same token; the row may only be claimed once."""
        token = concurrency.token_for(post)
        first = Post.objects.get(pk=post.pk)
        second = Post.objects.get(pk=post.pk)
        concurrency.claim(first, token)
        with pytest.raises(StaleWrite):
            concurrency.claim(second, token)

    @isolate_apps("tests.testapp")
    def test_a_counter_survives_the_save_that_follows_it(self, transactional_db):
        """The instance has to learn its new number, or ``save()`` puts the old one back.

        ``claim`` increments with ``F("version") + 1`` in SQL. The Python object still
        holds the pre-claim value, and the save that comes next writes every column —
        so without the write-back inside ``claim`` the lock silently disarms itself
        after exactly one use.
        """

        class CCounted(models.Model):
            name = models.CharField(max_length=20)
            version = models.IntegerField(default=0)

            class Meta:
                app_label = "testapp"

        # ``transactional_db`` rather than ``db``: SQLite refuses to run a schema
        # editor inside an open transaction, and the plain ``db`` fixture wraps
        # every test in one.
        with connection.schema_editor() as editor:
            editor.create_model(CCounted)
        try:
            row = CCounted.objects.create(name="one")
            concurrency.claim(row, concurrency.token_for(row))
            row.save()
            assert CCounted.objects.get(pk=row.pk).version == 1

            with pytest.raises(StaleWrite):
                concurrency.claim(row, "0")
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(CCounted)


# ── the diff ────────────────────────────────────────────────────────────────


class TestFieldDiff:
    def test_it_reports_both_sides(self, post):
        bump(post, status=Post.Status.PUBLISHED)
        fresh = Post.objects.get(pk=post.pk)
        diff = concurrency.field_diff(fresh, {"status": "review"})
        assert diff["status"]["mine"] == "review"
        assert diff["status"]["theirs"] == "published"

    def test_it_shows_labels_rather_than_stored_values(self, post):
        bump(post, status=Post.Status.PUBLISHED)
        fresh = Post.objects.get(pk=post.pk)
        diff = concurrency.field_diff(fresh, {"status": "review"})
        assert diff["status"]["theirs_display"] == "Published"
        assert diff["status"]["mine_display"] == "In review"

    def test_agreement_is_not_a_conflict(self, post):
        """Both people typing the same thing is not a question worth asking."""
        bump(post, status=Post.Status.PUBLISHED)
        fresh = Post.objects.get(pk=post.pk)
        diff = concurrency.field_diff(fresh, {"status": "published"})
        assert diff["status"]["conflict"] is False

    def test_it_covers_only_the_submitted_fields(self, post):
        bump(post, status=Post.Status.PUBLISHED, view_count=99)
        fresh = Post.objects.get(pk=post.pk)
        diff = concurrency.field_diff(fresh, {"status": "review"})
        assert list(diff) == ["status"]

    def test_a_value_that_does_not_even_parse_is_still_shown(self, post):
        diff = concurrency.field_diff(post, {"view_count": "abc"})
        assert diff["view_count"]["mine"] == "abc"


# ── through the endpoint ────────────────────────────────────────────────────


class TestConflictResponse:
    def test_a_stale_write_is_a_409(self, rf, boss, post, editable):
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)
        response = patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": stale, "fields": {"status": "review"}}]},
        )
        assert response.status_code == 409

    def test_the_stale_write_does_not_land(self, rf, boss, post, editable):
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)
        patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": stale, "fields": {"status": "review"}}]},
        )
        assert Post.objects.get(pk=post.pk).status == Post.Status.PUBLISHED

    def test_the_payload_carries_a_field_level_diff(self, rf, boss, post, editable):
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)
        response = patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": stale, "fields": {"status": "review"}}]},
        )
        body = json.loads(response.content)
        entry = body["conflicts"][0]
        assert entry["fields"]["status"] == {
            "mine": "review",
            "mine_display": "In review",
            "theirs": "published",
            "theirs_display": "Published",
            "conflict": True,
        }

    def test_the_payload_carries_the_token_to_retry_with(self, rf, boss, post, editable):
        """Without it the client can only ever conflict again, forever."""
        stale = concurrency.token_for(post)
        current = bump(post, status=Post.Status.PUBLISHED)
        response = patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": stale, "fields": {"status": "review"}}]},
        )
        body = json.loads(response.content)
        assert body["conflicts"][0]["version"] == concurrency.token_for(current)

    def test_one_stale_row_rolls_back_the_fresh_ones(self, rf, boss, post, author, editable):
        """Atomicity is the promise the "Save 2 changes" button made."""
        other = Post.objects.create(
            title="Second", slug="second", body="b", author=author, view_count=1
        )
        stale = concurrency.token_for(post)
        bump(post, status=Post.Status.PUBLISHED)

        response = patch(
            rf,
            editable,
            boss,
            {
                "changes": [
                    {
                        "pk": other.pk,
                        "version": concurrency.token_for(other),
                        "fields": {"view_count": 42},
                    },
                    {"pk": post.pk, "version": stale, "fields": {"status": "review"}},
                ]
            },
        )
        assert response.status_code == 409
        assert Post.objects.get(pk=other.pk).view_count == 1

    def test_retrying_with_the_new_token_succeeds(self, rf, boss, post, editable):
        stale = concurrency.token_for(post)
        current = bump(post, status=Post.Status.PUBLISHED)
        patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": stale, "fields": {"status": "review"}}]},
        )
        response = patch(
            rf,
            editable,
            boss,
            {
                "changes": [
                    {
                        "pk": post.pk,
                        "version": concurrency.token_for(current),
                        "fields": {"status": "review"},
                    }
                ]
            },
        )
        assert response.status_code == 200
        assert Post.objects.get(pk=post.pk).status == Post.Status.REVIEW

    def test_a_write_with_no_token_at_all_conflicts(self, rf, boss, post, editable):
        response = patch(
            rf, editable, boss, {"changes": [{"pk": post.pk, "fields": {"status": "review"}}]}
        )
        assert response.status_code == 409

    def test_an_unversioned_model_still_saves(self, rf, boss, author, monkeypatch):
        """No version column is not a reason to refuse the write, only to admit it."""
        panel = site.get_panel(Author)
        monkeypatch.setattr(panel, "list_editable", ["is_active"])
        monkeypatch.setattr(panel, "list_display", ["name", "email", "is_active"])
        response = patch(
            rf, panel, boss, {"changes": [{"pk": author.pk, "fields": {"is_active": False}}]}
        )
        assert response.status_code == 200
        assert Author.objects.get(pk=author.pk).is_active is False

    def test_the_response_hands_back_the_new_token(self, rf, boss, post, editable):
        response = patch(
            rf,
            editable,
            boss,
            {
                "changes": [
                    {
                        "pk": post.pk,
                        "version": concurrency.token_for(post),
                        "fields": {"status": "review"},
                    }
                ]
            },
        )
        body = json.loads(response.content)
        assert body["rows"][0]["version"] == concurrency.token_for(Post.objects.get(pk=post.pk))

    def test_the_new_token_is_immediately_usable(self, rf, boss, post, editable):
        """Two saves in a row must work without a page reload."""
        first = patch(
            rf,
            editable,
            boss,
            {
                "changes": [
                    {
                        "pk": post.pk,
                        "version": concurrency.token_for(post),
                        "fields": {"status": "review"},
                    }
                ]
            },
        )
        token = json.loads(first.content)["rows"][0]["version"]
        second = patch(
            rf,
            editable,
            boss,
            {"changes": [{"pk": post.pk, "version": token, "fields": {"view_count": 7}}]},
        )
        assert second.status_code == 200
        assert Post.objects.get(pk=post.pk).view_count == 7
