#!/usr/bin/env python3
"""Build ``docs/llms.txt`` and ``docs/llms-full.txt`` from the docs site.

Both files follow the llmstxt.org convention so a coding agent can read the documentation
without crawling the site:

* ``llms.txt`` is an index: a title, a one-paragraph summary, then ``- [Title](url): one
  line`` links grouped by the sections of the ``nav`` in ``mkdocs.yml``.
* ``llms-full.txt`` is the full text of every page in nav order, each preceded by its URL.

Only pages listed in ``nav`` are included, so internal and planning documents under
``docs/`` that are not part of the published site never reach either file.

    python scripts/build_llms_txt.py           # regenerate both files
    python scripts/build_llms_txt.py --check   # exit 1 if the committed files are stale

The output is deterministic: the same docs always produce the same bytes. A test runs
``--check``, so the files cannot drift from the docs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Used when ``mkdocs.yml`` has no ``site_url``.
DEFAULT_SITE_URL = "https://pythondeveloper6.github.io/django-vantage/"

#: Top-level nav entries that are single pages share this heading.
OVERVIEW = "Overview"

#: Longest page description in ``llms.txt``, in characters.
MAX_DESCRIPTION = 240

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_NAV_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")


def read_scalar(mkdocs_text: str, key: str) -> str | None:
    """A top-level ``key: value`` from ``mkdocs.yml``, without a YAML parser.

    ``mkdocs.yml`` carries Python-specific tags that ``yaml.safe_load`` rejects, and this
    script must run with nothing installed, so it reads only the two shapes it needs.
    """
    for line in mkdocs_text.splitlines():
        match = _TOP_KEY.match(line)
        if match and match.group(1) == key:
            return match.group(2).strip().strip("\"'") or None
    return None


def parse_nav(mkdocs_text: str) -> list[tuple[str | None, str, str]]:
    """The ``nav`` as ``(section, title, path)`` rows, in nav order.

    ``section`` is ``None`` for a page at the top level. Only one level of nesting is
    understood, which is all this site uses; a deeper level raises rather than guessing.
    """
    lines = mkdocs_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "nav:")
    except StopIteration:
        raise SystemExit("mkdocs.yml has no nav:") from None

    rows: list[tuple[str | None, str, str]] = []
    section: str | None = None
    section_indent = -1
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            break  # the next top-level key
        match = _NAV_ITEM.match(line)
        if not match:
            continue
        indent, body = len(match.group(1)), match.group(2)
        title, _, target = body.partition(":")
        title, target = title.strip().strip("\"'"), target.strip().strip("\"'")
        if not target:
            section, section_indent = title, indent
            continue
        if section is not None and indent <= section_indent:
            section = None
        if section is not None and indent > section_indent + 2 * 2:
            raise SystemExit(f"nav nesting deeper than one level at: {line.strip()}")
        rows.append((section, title, target))
    return rows


def page_url(site_url: str, path: str) -> str:
    """The published URL of a docs page (mkdocs' directory URLs)."""
    base = site_url if site_url.endswith("/") else site_url + "/"
    stem = path[:-3] if path.endswith(".md") else path
    if stem == "index":
        return base
    if stem.endswith("/index"):
        stem = stem[: -len("index")]
        return base + stem
    return f"{base}{stem}/"


def strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)(\{[^}]*\})?")
_INLINE = re.compile(r"[*_`]+")
_HTML = re.compile(r"<[^>]+>")


def first_paragraph(text: str) -> str:
    """The first prose paragraph after the page's heading, as one plain line."""
    body = strip_front_matter(text)
    paragraph: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        skip = (
            in_fence
            or stripped.startswith(("#", "!!!", "???", "<", "|", "{", "[←", "[![", "- ", "* ", ">"))
            or re.match(r"^\d+\.\s", stripped)
            or stripped.startswith("    ")
        )
        if not stripped:
            if paragraph:
                break
            continue
        if skip:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    plain = " ".join(paragraph)
    plain = _LINK.sub(r"\1", plain)
    plain = _HTML.sub("", plain)
    plain = _INLINE.sub("", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) > MAX_DESCRIPTION:
        cut = plain[:MAX_DESCRIPTION]
        sentence = max(cut.rfind(". "), cut.rfind("? "))
        plain = (
            cut[: sentence + 1] if sentence > 60 else cut.rsplit(" ", 1)[0].rstrip(",;:") + "..."
        )
    return plain


def build(root: Path = ROOT, mkdocs: Path | None = None, docs: Path | None = None):
    """Return ``(llms_txt, llms_full_txt)`` for the docs under ``root``."""
    mkdocs = mkdocs or root / "mkdocs.yml"
    docs = docs or root / "docs"
    text = mkdocs.read_text(encoding="utf-8")
    site_url = read_scalar(text, "site_url") or DEFAULT_SITE_URL
    title = read_scalar(text, "site_name") or "Documentation"
    summary = read_scalar(text, "site_description") or ""

    pages = []
    for section, page_title, path in parse_nav(text):
        if path.startswith(("http://", "https://")) or path.startswith("superpowers/"):
            continue
        source = docs / path
        if not source.is_file():
            raise SystemExit(f"mkdocs.yml nav lists {path}, which does not exist in {docs}")
        pages.append((section or OVERVIEW, page_title, path, source.read_text(encoding="utf-8")))

    sections: dict[str, list[tuple[str, str, str, str]]] = {}
    for section, page_title, path, content in pages:
        sections.setdefault(section, []).append((page_title, path, content, ""))
    order = sorted(sections, key=lambda name: (name != OVERVIEW, list(sections).index(name)))

    index = [f"# {title}", ""]
    if summary:
        index += [f"> {summary}", ""]
    index += [
        f"Full text of every page below, in one file: {site_url.rstrip('/')}/llms-full.txt",
        "",
    ]
    for name in order:
        index += [f"## {name}", ""]
        for page_title, path, content, _ in sections[name]:
            line = f"- [{page_title}]({page_url(site_url, path)})"
            blurb = first_paragraph(content)
            index.append(f"{line}: {blurb}" if blurb else line)
        index.append("")

    full = [f"# {title}", ""]
    if summary:
        full += [f"> {summary}", ""]
    for _, _title, path, content in pages:
        full += [
            "---",
            "",
            f"URL: {page_url(site_url, path)}",
            "",
            strip_front_matter(content).strip("\n"),
            "",
        ]
    return "\n".join(index).rstrip("\n") + "\n", "\n".join(full).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="Exit 1 if the files are stale.")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="Project root (default: this repo)."
    )
    args = parser.parse_args(argv)

    root = args.root
    docs = root / "docs"
    index, full = build(root)
    targets = {docs / "llms.txt": index, docs / "llms-full.txt": full}

    if args.check:
        stale = [
            str(path.relative_to(root))
            for path, content in targets.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            print(
                f"Stale: {', '.join(stale)}. Run: python scripts/build_llms_txt.py",
                file=sys.stderr,
            )
            return 1
        return 0

    for path, content in targets.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
