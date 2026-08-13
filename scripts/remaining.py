#!/usr/bin/env python3
"""What is still not built.

Run this at every stopping point — it reads the checkboxes out of PLAN.md and prints
what is left, so picking up the next piece of work never depends on remembering.

    python scripts/remaining.py            # summary + next actionable items
    python scripts/remaining.py --all      # every unchecked item
    python scripts/remaining.py --section 4.3
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent / "PLAN.md"

BOX = re.compile(r"^\s*-\s*\[( |x|~)\]\s*(.+?)\s*$")
HEADING = re.compile(r"^(#{2,3})\s+(.*)$")


def parse(text: str) -> list[dict]:
    """Every checkbox in the file, tagged with the heading it sits under."""
    items: list[dict] = []
    section = "(none)"
    pending: dict | None = None

    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            section = heading.group(2).strip()
            pending = None
            continue

        box = BOX.match(line)
        if box:
            state = {" ": "todo", "x": "done", "~": "partial"}[box.group(1)]
            pending = {"state": state, "text": box.group(2), "section": section}
            items.append(pending)
        elif pending is not None and line.startswith(("      ", "\t")):
            # A wrapped continuation line belongs to the item above it.
            pending["text"] += " " + line.strip()
        elif not line.strip():
            pending = None

    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="list every unchecked item")
    parser.add_argument("--section", help="filter to sections containing this string")
    args = parser.parse_args()

    if not PLAN.exists():
        print(f"No plan at {PLAN}", file=sys.stderr)
        return 1

    items = parse(PLAN.read_text(encoding="utf-8"))
    if args.section:
        needle = args.section.lower()
        items = [i for i in items if needle in i["section"].lower()]

    done = [i for i in items if i["state"] == "done"]
    partial = [i for i in items if i["state"] == "partial"]
    todo = [i for i in items if i["state"] == "todo"]

    total = len(items)
    pct = (len(done) / total * 100) if total else 0.0
    print(f"{len(done)}/{total} done ({pct:.0f}%) · {len(partial)} partial · {len(todo)} todo\n")

    if partial:
        print("IN PROGRESS — finish these before starting anything new")
        for item in partial:
            print(f"  ~ {item['text']}")
        print()

    by_section: dict[str, list[dict]] = {}
    for item in todo:
        by_section.setdefault(item["section"], []).append(item)

    shown = 0
    for section, entries in by_section.items():
        print(f"{section}  ({len(entries)} left)")
        for item in entries if args.all else entries[:3]:
            print(f"  - {item['text'][:100]}")
            shown += 1
        if not args.all and len(entries) > 3:
            print(f"  … {len(entries) - 3} more")
        print()

    if not args.all and shown < len(todo):
        print("Run with --all to see everything.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
