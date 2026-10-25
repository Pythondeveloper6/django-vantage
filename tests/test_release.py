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
