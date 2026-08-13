#!/usr/bin/env bash
# Bring the demo up from nothing: apply migrations, seed the database, serve it.
#
# Safe to re-run — the migration and the seed are both idempotent, so this is also the
# way to get back to a known-good database after poking at one.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run python example/manage.py migrate
uv run python example/manage.py seed

echo
echo "Panel:  http://127.0.0.1:8765/admin/"
echo "Login:  admin / admin12345"
echo

exec uv run python example/manage.py runserver 8765
