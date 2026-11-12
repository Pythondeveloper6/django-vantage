"""``docs/llms.txt`` and ``docs/llms-full.txt`` must match the docs they are built from."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_llms_txt.py"

spec = importlib.util.spec_from_file_location("build_llms_txt", SCRIPT)
build_llms_txt = importlib.util.module_from_spec(spec)
sys.modules["build_llms_txt"] = build_llms_txt
spec.loader.exec_module(build_llms_txt)


def make_project(tmp_path):
    docs = tmp_path / "docs"
    (docs / "superpowers").mkdir(parents=True)
    (docs / "index.md").write_text("---\ntitle: x\n---\n# Home\n\nWelcome to **Shop**.\n")
    (docs / "a.md").write_text(
        "# A\n\n[← Index](index.md)\n\nThe `A` page, see [b](b.md).\n\nMore.\n"
    )
    (docs / "b.md").write_text("# B\n\n```\ncode\n```\n\nBee text.\n")
    (docs / "secret.md").write_text("# Not in nav\n\nPlanning.\n")
    (docs / "superpowers" / "plan.md").write_text("# Plan\n\nInternal.\n")
    (tmp_path / "mkdocs.yml").write_text(
        "site_name: demo\nsite_description: A demo.\nsite_url: https://example.test/d/\n"
        "nav:\n  - Home: index.md\n  - Guide:\n      - Page A: a.md\n      - Page B: b.md\n"
        "theme:\n  name: material\n"
    )
    return tmp_path


def test_committed_files_are_current():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr


def test_build_is_deterministic_and_follows_the_nav(tmp_path):
    project = make_project(tmp_path)
    index, full = build_llms_txt.build(project)
    assert build_llms_txt.build(project) == (index, full)
    assert index.startswith("# demo\n\n> A demo.\n")
    assert "- [Home](https://example.test/d/): Welcome to Shop." in index
    assert "- [Page A](https://example.test/d/a/): The A page, see b." in index
    assert "- [Page B](https://example.test/d/b/): Bee text." in index
    assert index.index("## Overview") < index.index("## Guide")
    assert "Planning" not in full and "Internal" not in full
    assert full.index("URL: https://example.test/d/a/") < full.index(
        "URL: https://example.test/d/b/"
    )
    assert "title: x" not in full


def test_check_mode_detects_stale_files(tmp_path):
    project = make_project(tmp_path)
    args = [sys.executable, str(SCRIPT), "--root", str(project)]
    assert subprocess.run([*args, "--check"], capture_output=True).returncode == 1
    assert subprocess.run(args, capture_output=True).returncode == 0
    assert subprocess.run([*args, "--check"], capture_output=True).returncode == 0
    (project / "docs" / "b.md").write_text("# B\n\nChanged.\n")
    assert subprocess.run([*args, "--check"], capture_output=True).returncode == 1
