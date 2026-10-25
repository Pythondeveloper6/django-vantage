# Contributing to django-vantage

Thank you for considering it. This document is the whole of what you need to know:
how to get a working environment, how to run the checks, the four conventions this
codebase actually enforces, and where to put a new test case.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Set up

The project uses [uv](https://docs.astral.sh/uv/). Install it first — everything else
follows from one command.

```bash
git clone https://github.com/Pythondeveloper6/django-vantage
cd django-vantage
uv sync --all-groups
```

That creates `.venv/`, installs the package in editable mode and pulls the dev group
(pytest, pytest-django, pytest-cov, ruff, model-bakery). You never need to activate the
virtualenv: prefix commands with `uv run`.

No Node.js, no asset build, no `npm install`. The CSS and JavaScript in
`src/vantage/static/` are hand-written and committed, and that is deliberate — a package
whose premise is `pip install` and nothing else cannot require a bundler to contribute
to.

## Run the tests

```bash
uv run pytest                     # the whole suite
uv run pytest tests/test_inference.py
uv run pytest -k "soft_delete"
uv run pytest -x -q               # stop at the first failure
uv run pytest --cov=vantage --cov-report=term-missing
```

Settings come from `tests/settings.py` via `pytest-django`; there is no `manage.py` in
the loop and no database file — the suite runs against in-memory SQLite.

## Lint

```bash
uv run ruff check src tests       # this is what CI gates on
uv run ruff check --fix src tests
```

`ruff format` is **not** yet enforced. The tree has never been run through it, so
enabling the gate today would fail every pull request for reasons unrelated to the
change; CI runs `ruff format --check` in advisory mode so the drift stays visible. Don't
reformat files you aren't otherwise touching.

Optional, and worth installing once:

```bash
uvx pre-commit install
```

## Type checking

```bash
uvx ty check src
```

Advisory, and honestly so — it currently reports a few hundred diagnostics, most of them
`unresolved-attribute` on Django's model and queryset descriptors, which no static
checker resolves without stubs. Don't let it stop you, and don't add `# type: ignore`
in bulk to quiet it.

## Run the matrix locally

CI tests Python 3.12/3.13/3.14 against Django 5.2 (LTS) and 6.1, plus Django `main` as
an allowed failure. You can run any cell of that yourself:

```bash
uvx --with tox-uv tox                  # everything
uvx --with tox-uv tox -e py313-dj52    # one cell
uvx --with tox-uv tox -e lint,build
uvx --with tox-uv tox -e py313-dj61 -- -k inference -x
```

uv downloads any interpreter you don't have, so this works on a machine with one Python.
`tox -e build` is the packaging gate — see [Packaging](#packaging) below.

## Run the demo

A full demo project, "Northwind Supply", lives in `example/`:

```bash
./example/run.sh          # migrate, seed, serve on http://127.0.0.1:8765/admin/
```

Sign in as `admin` / `admin12345`. The script is idempotent, so re-running it is also
how you get back to a clean database.

Render the page and look at it. More than one bug in this repository's history was a
green suite over markup that had silently vanished, because no test asserted on it.

---

## Conventions

Four of them are enforced by tests rather than by review. If you break one, the suite
tells you before a human does.

### 1. Docstrings explain *why*, not *what*

The signature already says what a function takes. A docstring earns its place by
recording the decision — the alternative that was rejected, the trap that made this
shape necessary, the failure mode being avoided. If you delete the docstring and lose no
information, it was not a docstring.

```python
def has_object_perm(self, request, perm, obj):
    """Short-circuits to the model answer when no backend can answer object-level.

    Django's ModelBackend returns an empty permission set whenever ``obj`` is not
    None, so without this every object check on a stock project would be False and
    no row would ever be actionable.
    """
```

The same applies to commit messages. `git log` in this repository is the design record,
and it is expected to explain the reasoning, name the alternatives considered, and
record the mistakes made on the way. Read a few before writing your first.

### 2. Logical CSS properties only

Never `margin-left`, `padding-right`, `left:` or `right:`. Use `margin-inline-start`,
`padding-inline-end`, `inset-inline-start`, `inset-inline-end`. RTL support works from
`dir="rtl"` and nothing else, which only holds if nothing is pinned to a physical side.

This is enforced: `tests/test_stylesheets.py::test_no_physical_directions` scans every
file in `src/vantage/static/vantage/*.css`. RTL breaks *silently* — the page still
renders, just mirrored wrongly, and nobody reading English ever notices. That is why
there is a guard instead of a style note.

### 3. Every colour comes from a token

No hex literals, no `rgb()`, no `hsl()` in a stylesheet. Every colour reads a CSS custom
property defined by the theme layer, so `BRAND_COLOR` restyles the whole panel and dark
mode is a token swap rather than a second stylesheet.

```css
/* no */   color: #64748b;
/* yes */  color: var(--v-muted-foreground);
```

Enforced by `tests/test_stylesheets.py::test_no_hard_coded_colours`. There is exactly
one exception, also enforced: a **neutral** shadow or scrim written as
`oklch(0 0 0 / …)`, because a shadow that follows the brand hue looks wrong. Nothing
else may bypass the token layer.

If you need a colour that doesn't exist yet, add the token in the theme layer and give
it a *semantic* name — a role, not a colour. `--v-destructive`, not `--v-red`.

Two more properties the same file guards: anything that animates must respect
`prefers-reduced-motion`, and every token must be defined on bare `:root` and only
*redefined* under the dark rules — a colour whose only definition lives inside
`@media (prefers-color-scheme: dark)` makes the theme toggle work in one direction only.

### 4. Queries do not grow with rows

The package's central promise is that a page costs the same whether it renders five rows
or two hundred. If you add something that touches the database per row, a test should
fail; if it doesn't, add the test. The pattern used throughout:

```python
def test_the_page_cost_does_not_grow_with_the_row_count(self, client_in):
    with assertNumQueries(...) as small: ...     # 5 rows
    with assertNumQueries(...) as large: ...     # 205 rows
    assert small == large
```

Derive `select_related`/`prefetch_related` from the paths the page actually renders
(`vantage.optimizer.plan_related`) rather than hand-maintaining a list, so the
optimisation cannot drift out of date with the display.

---

## Adding an inference case

Inference is the core value of the package, so it carries the densest coverage. **The
way to add an inference test case is to add a model to `tests/testapp/models.py`.**

That app exists to cover a wide spread of field types, relationship shapes and naming
conventions. A new shape goes there as a normal Django model, and then the assertion
goes in `tests/test_inference.py`:

```python
# tests/testapp/models.py
class Invoice(models.Model):
    """Composite-key-ish natural identifier — exercises link-column inference."""

    reference = models.CharField(max_length=32, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    api_token = models.CharField(max_length=64)   # must never be inferred anywhere
    issued_at = models.DateField()

    def __str__(self):
        return self.reference
```

```python
# tests/test_inference.py
def test_a_reference_column_is_the_link_not_the_total():
    assert infer_list_link(Invoice) == "reference"
```

Things to know before you do:

- **A new model changes the fixture app's shape.** Several tests assert on properties of
  the whole registry (how many models exist, dashboard card caps, nav ordering). They
  are written to assert *properties* rather than specific names precisely so that adding
  a model is cheap, but run the full suite, not just `test_inference.py`.
- **No migrations.** `tests/testapp` has no migrations directory; pytest-django creates
  the schema from the models directly.
- **Give the model a `__str__`** unless the point of the case is that it lacks one.
- **Name the fields the way real projects do.** Inference reads names, so
  `published_at` versus `created_at` versus `date` is the substance of the test, not
  incidental detail. A comment saying which convention the model is exercising is worth
  writing.
- If the case is about *pattern detection* (soft delete, hierarchy, timeline,
  publishing, money) rather than field-to-widget inference, the assertions belong in
  `tests/test_patterns.py`, but the model still goes in the same place.

Ask the engine what it decided at any point:

```bash
uv run python example/manage.py vantage_scaffold shop
```

---

## Packaging

`vantage` is mostly *not* Python: 30 templates, 16 static files and a templatetags
module, every one located by path at runtime rather than imported. A wheel that dropped
them would still import, still pass `twine check`, still install cleanly, and 500 on the
first page view. So there is a check that opens the archive:

```bash
uv build
uvx twine check --strict dist/*
uv run python scripts/verify_wheel.py dist/*.whl
```

The last one installs the wheel into a throwaway virtualenv with nothing else in it,
boots Django with `vantage` in `INSTALLED_APPS`, compiles every shipped template through
the app-dirs loader, renders one for real, and resolves the static files through the
staticfiles finder — from a scratch directory, so `src/` cannot stand in for anything
the wheel left out.

**If you add a new kind of file to `src/vantage/`** — a locale directory, a JSON data
file, a `py.typed` marker — check it actually ships. The build backend is hatchling and
the include rules are in `pyproject.toml` under `[tool.hatch.build.targets.*]`.
`MANIFEST.in` is present for tooling that expects it but is **not** read by hatchling;
pyproject is the authority.

---

## Pull requests

- Branch from `main`. One concern per pull request.
- `uv run pytest` and `uv run ruff check src tests` both clean.
- Add the entry to `CHANGELOG.md` under `## [Unreleased]`, in the right subsection.
- Write the commit message the way the existing ones are written: what the change does,
  why that shape and not the obvious alternative, and anything you got wrong on the way.
  A reader six months from now is the audience.
- New behaviour needs a test. New *user-visible* behaviour needs a line in the README
  too, if the README would otherwise be wrong.

## Reporting a bug

Open an issue with the Django and Python versions, the model or panel that reproduces
it, and what you expected instead. A failing test is the best possible bug report.

Security issues do **not** go in the issue tracker — see [SECURITY.md](SECURITY.md).
