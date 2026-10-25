"""Release metadata that a person copies and pastes.

Every URL here is one a new user follows before they have read a line of the code —
the install command in the README, the clone command in CONTRIBUTING, the advisory
link in SECURITY. A placeholder that survives to a release sends them to someone
else's repository, so the check is mechanical rather than left to review.
"""

import re
import tomllib
from pathlib import Path

import pytest

import vantage

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = "github.com/Pythondeveloper6/django-vantage"

#: Everything a user or a packaging tool reads. Tests, the lockfile and the demo
#: project are not here: they name GitHub only for third-party projects.
SURFACES = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
    ".github/workflows/release.yml",
    *sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "docs").glob("*.md")),
    *sorted(str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob("src/vantage/locale/*/*/*.po")),
]

# Any github.com/<owner>/django-vantage — the repository name is what identifies a
# reference to this project, whoever the owner is written as.
OWN_REPO = re.compile(r"github\.com/([\w.-]+)/django-vantage", re.IGNORECASE)


@pytest.mark.parametrize("relpath", SURFACES)
def test_every_reference_to_this_repository_names_the_real_one(relpath):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    wrong = sorted({m.group(0) for m in OWN_REPO.finditer(text)} - {CANONICAL})
    assert not wrong, f"{relpath} points at {wrong}, not {CANONICAL}"


@pytest.mark.parametrize("relpath", SURFACES)
def test_no_placeholder_owner_survives(relpath):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert "your-org" not in text


def test_project_urls_all_point_at_the_canonical_repository():
    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    urls = meta["project"]["urls"]
    assert urls, "pyproject.toml has no [project.urls]"
    for label, url in urls.items():
        assert url.startswith(f"https://{CANONICAL}"), f"{label} = {url}"


def test_the_changelog_has_an_entry_for_the_packaged_version():
    """A release whose changelog does not mention it ships with no notes at all —
    the GitHub release body points at CHANGELOG.md."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if ".dev" in vantage.__version__:
        pytest.skip("development versions are covered by [Unreleased]")
    assert f"## [{vantage.__version__}]" in changelog


# ── documentation that states a fact about the code ─────────────────────────────────


def _complete_list() -> set[str]:
    text = (REPO_ROOT / "docs/settings.md").read_text(encoding="utf-8")
    section = text.split("## The complete list", 1)[1]
    block = section.split("```", 2)[1]
    return set(block.split())


def test_the_settings_page_lists_exactly_the_registered_keys():
    """docs/settings.md enumerates every key; a key added to DEFAULTS without it is a
    setting nobody can discover, and a key listed but not registered is one that takes
    the site down when written (the registry rejects unknown keys)."""
    from vantage.conf import DEFAULTS

    listed = _complete_list()
    assert listed == set(DEFAULTS), {
        "registered, not listed": sorted(set(DEFAULTS) - listed),
        "listed, not registered": sorted(listed - set(DEFAULTS)),
    }


def test_the_faq_does_not_claim_the_shell_is_unnonced():
    """It once said 'no nonce is emitted' while base.html nonced both inline blocks."""
    faq = (REPO_ROOT / "docs/faq.md").read_text(encoding="utf-8")
    assert "no nonce is emitted" not in faq
    assert "csp_nonce_attr" in faq


# ── the support matrix ──────────────────────────────────────────────────────────────


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _classified_djangos() -> set[str]:
    prefix = "Framework :: Django :: "
    classifiers = _pyproject()["project"]["classifiers"]
    return {c[len(prefix) :] for c in classifiers if c.startswith(prefix)}


def test_every_classified_django_is_a_ci_cell():
    """A classifier is a promise of support; CI is the evidence. Django main is an
    allowed failure and is deliberately not classified."""
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    cells = re.search(r'^\s*django: \[([^\]]*)\]', ci, re.MULTILINE)
    assert cells, "could not find the django matrix in ci.yml"
    tested = {v.strip().strip('"') for v in cells.group(1).split(",")}
    assert _classified_djangos() == tested


def test_every_classified_django_is_a_tox_env():
    tox = (REPO_ROOT / "tox.ini").read_text(encoding="utf-8")
    envs = re.search(r"py\{[\d,]+\}-dj\{([\d,]+)\}", tox)
    assert envs
    tested = {f"{v[0]}.{v[1:]}" for v in envs.group(1).split(",")}
    assert _classified_djangos() == tested


def test_the_django_requirement_has_an_upper_bound_just_past_the_newest_tested():
    """Without the bound, pip pairs this release with the next Django on the day it
    ships, which nobody has run the suite against."""
    (dep,) = [d for d in _pyproject()["project"]["dependencies"] if d.lower().startswith("django")]
    bound = re.search(r"<\s*(\d+)\.(\d+)", dep)
    assert bound, f"{dep} has no upper bound"
    newest = max(_classified_djangos(), key=lambda v: tuple(map(int, v.split("."))))
    major, minor = map(int, newest.split("."))
    assert (int(bound.group(1)), int(bound.group(2))) == (major, minor + 1), dep


def test_the_version_has_one_source():
    project = _pyproject()["project"]
    assert "version" not in project
    assert "version" in project.get("dynamic", [])
