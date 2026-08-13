#!/usr/bin/env python3
"""Prove a built wheel is actually installable and actually complete.

A Django app is mostly not Python. `vantage` ships 30 templates, 16 static files and a
templatetags module, and every one of them is loaded by *path at runtime* rather than
imported — so a build backend that quietly drops them produces a wheel that imports
fine, passes ``twine check``, installs without complaint, and 500s on the first page
view. That failure mode is the reason this script exists: nothing else in the pipeline
looks inside the archive.

Two phases, deliberately separate:

1. **inspect** — read the zip with the standard library only, from outside any venv,
   and assert the non-Python payload is present. Cheap, and it fails before the slow
   part when the build config has regressed.
2. **smoke** — install the wheel into a throwaway venv that has nothing else in it and
   exercise it as a consumer would: import the package, boot Django with ``vantage`` in
   ``INSTALLED_APPS``, compile every shipped template through the app-dirs loader, and
   render one of them for real. Running from a scratch directory matters — from the
   repo root ``src/`` and ``tests/`` are on hand to mask a missing file.

    python scripts/verify_wheel.py dist/*.whl        # both phases
    python scripts/verify_wheel.py --inspect-only dist/*.whl
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

#: Floors, not exact counts. An exact count would fail every time someone adds a
#: template, which trains people to edit the number rather than read the failure.
#: These are set at roughly the current inventory so a *collapse* is still caught.
MIN_TEMPLATES = 25
MIN_STATIC = 12

#: Loaded by path at runtime, so a missing one is invisible until a page is served.
REQUIRED_FILES = [
    "vantage/templates/vantage/base.html",
    "vantage/templates/vantage/list.html",
    "vantage/templates/vantage/partials/stat_card.html",
    "vantage/static/vantage/vantage.css",
    "vantage/static/vantage/vantage.js",
    "vantage/templatetags/__init__.py",
    "vantage/templatetags/vantage.py",
    "vantage/migrations/0001_initial.py",
]

# Run inside the throwaway venv. Kept as a string rather than a module so the venv
# never needs the repository on its path — which is the whole point of the exercise.
SMOKE = '''
import django
from django.conf import settings

settings.configure(
    SECRET_KEY="wheel-smoke-test",
    DEBUG=False,
    USE_TZ=True,
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.staticfiles",
        "vantage",
    ],
    STATIC_URL="/static/",
    TEMPLATES=[{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }],
)
django.setup()

import vantage
print("import vantage           :", vantage.__file__)
print("vantage.__version__      :", vantage.__version__)

# The lazy re-exports are the public API; a missing submodule surfaces here.
from vantage import ModelPanel, register, site  # noqa: F401
print("public API               : ModelPanel, register, site all resolve")

# Every shipped template, found through the app-dirs loader exactly as a host project
# would find it. compile != render, but it proves the file is on disk and parses.
from pathlib import Path
from django.template.loader import get_template, render_to_string

root = Path(vantage.__file__).parent
names = sorted(
    p.relative_to(root / "templates").as_posix()
    for p in (root / "templates").rglob("*.html")
)
if not names:
    raise SystemExit("FAIL: no templates found in the installed package")
for name in names:
    get_template(name)
print(f"templates compiled       : {len(names)}")

# One real render, through a template that loads the packaged templatetags library —
# so this covers the tag module shipping too, not only the .html files.
html = render_to_string(
    "vantage/partials/stat_card.html",
    {"stat": {"label": "Revenue", "display": "$227,298.26"}},
)
assert "Revenue" in html and "$227,298.26" in html, html
print("rendered stat_card.html  :", " ".join(html.split())[:96])

# Static files resolved through the finder, which is how collectstatic will see them.
from django.contrib.staticfiles import finders

statics = sorted(p.name for p in (root / "static" / "vantage").iterdir())
missing = [n for n in statics if finders.find(f"vantage/{n}") is None]
if missing:
    raise SystemExit(f"FAIL: staticfiles finder missed {missing}")
print(f"static files found       : {len(statics)}")

# Migrations have to be loadable or the app cannot be installed into a real project.
from django.db.migrations.loader import MigrationLoader

loader = MigrationLoader(None, ignore_no_migrations=True)
applied = sorted(name for app, name in loader.disk_migrations if app == "vantage")
print("migrations loadable      :", ", ".join(applied))

print("SMOKE OK")
'''


def inspect(wheel: Path) -> None:
    """Assert the archive contains the parts that are loaded by path, not by import."""
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    templates = [n for n in names if n.startswith("vantage/templates/") and n.endswith(".html")]
    static = [n for n in names if n.startswith("vantage/static/")]
    python = [n for n in names if n.endswith(".py")]

    print(f"wheel                    : {wheel.name}")
    print(f"  python modules         : {len(python)}")
    print(f"  templates (.html)      : {len(templates)}")
    print(f"  static files           : {len(static)}")

    problems = []
    if len(templates) < MIN_TEMPLATES:
        problems.append(f"only {len(templates)} templates, expected >= {MIN_TEMPLATES}")
    if len(static) < MIN_STATIC:
        problems.append(f"only {len(static)} static files, expected >= {MIN_STATIC}")
    for required in REQUIRED_FILES:
        if required not in names:
            problems.append(f"missing {required}")

    # Compiled bytecode in a wheel is stale the moment the interpreter differs, and it
    # bloats the download; its presence means the build read a dirty working tree.
    if any(n.endswith(".pyc") or "__pycache__" in n for n in names):
        problems.append("wheel contains __pycache__/.pyc — the build read a dirty tree")

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        raise SystemExit(1)
    print("  inspect                : OK")


def smoke(wheel: Path, python_version: str) -> None:
    """Install into an empty venv and use the package the way a host project does."""
    if shutil.which("uv") is None:
        raise SystemExit("FAIL: uv is required (https://docs.astral.sh/uv/)")

    # Inherited UV_*/VIRTUAL_ENV/PYTHONPATH turn "a venv with nothing in it" into "the
    # project's venv wearing a disguise", which would let the repository satisfy every
    # import the wheel forgot. Strip them.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("UV_") and k not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }

    with tempfile.TemporaryDirectory(prefix="vantage-wheel-") as tmp:
        work = Path(tmp)
        venv = work / "venv"
        script = work / "smoke.py"
        script.write_text(SMOKE, encoding="utf-8")

        run = lambda argv: subprocess.run(argv, check=True, cwd=work, env=env)  # noqa: E731
        # Pinned explicitly: `uv venv` with no version picks whatever `python3` the
        # scratch directory resolves to, which on a developer machine is routinely an
        # older interpreter than the package supports.
        run(["uv", "venv", "--quiet", "--python", python_version, str(venv)])
        python = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        run(["uv", "pip", "install", "--quiet", "--python", str(python), str(wheel.resolve())])
        # cwd is the scratch directory on purpose: from the repo root, `src/` and
        # `tests/` would be available to paper over anything the wheel left out.
        run([str(python), str(script)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path, help="path to the built .whl")
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="skip the venv install (useful when there is no network)",
    )
    parser.add_argument(
        "--python",
        default=f"{sys.version_info.major}.{sys.version_info.minor}",
        help="interpreter version for the throwaway venv (default: the one running this)",
    )
    args = parser.parse_args()

    if not args.wheel.is_file():
        raise SystemExit(f"FAIL: no such wheel: {args.wheel}")

    inspect(args.wheel)
    if not args.inspect_only:
        smoke(args.wheel, args.python)
    print("wheel verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
