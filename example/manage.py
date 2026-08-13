#!/usr/bin/env python
"""Entry point for the demo project.

Run it from anywhere — ``uv run python example/manage.py check`` works from the repo
root because Python puts this file's directory on ``sys.path``, which is what makes
``config`` and ``shop`` importable without an installed package or a PYTHONPATH.
"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
