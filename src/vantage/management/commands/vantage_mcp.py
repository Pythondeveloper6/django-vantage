"""``manage.py vantage_mcp --agent NAME`` - the MCP server over standard input/output.

Acts as the named agent identity without a token: the process is one the operator started
themselves. Only protocol messages are written to stdout; everything else goes to stderr.
See :mod:`vantage.mcp`.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from vantage import mcp
from vantage.models import AgentIdentity


class Command(BaseCommand):
    help = "Serve the Vantage MCP server on stdin/stdout as an agent identity."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--agent", required=True, help="Name of the agent identity.")

    def handle(self, *args, **options):
        def fail(message):
            sys.stderr.write(f"vantage_mcp: {message}\n")
            sys.stderr.flush()
            raise SystemExit(1)

        identity = (
            AgentIdentity.objects.select_related("user", "owner")
            .filter(name=options["agent"])
            .first()
        )
        if identity is None:
            fail(f"no agent called {options['agent']!r}.")
        try:
            base = mcp.stdio_request(identity)
        except mcp.StdioRefused as exc:
            fail(str(exc))

        from vantage import site

        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
        sys.stderr.write(f"vantage_mcp: serving as agent {identity.name!r}\n")
        sys.stderr.flush()
        mcp.serve_stdio(mcp.McpServer(site, base), sys.stdin, sys.stdout)
