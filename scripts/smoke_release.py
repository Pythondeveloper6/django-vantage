"""Happy-path release smoke test: run the demo project end to end, as a user would.

Everything happens in a temporary directory with a throwaway SQLite database and media
folder; nothing in the checkout is touched. The script migrates and seeds the demo
(``example/``), starts ``runserver`` on a free port, signs in through the real login form
(cookie jar and CSRF token), then walks the pages, forms, exports and JSON API. It then
restarts the server with agent identities switched on and exercises the bearer-token API,
the MCP endpoint and the stdio MCP server. It finishes with ``check``, ``vantage_describe``
and ``vantage_upgrade_check``.

    uv run python scripts/smoke_release.py

One PASS or FAIL line per step; the exit status is non-zero if any step failed. The server
is always stopped on the way out. Standard library only, plus the package itself.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "example"
ADMIN = "/admin"
API = f"{ADMIN}/_/api"

RESULTS: list[tuple[str, bool, str]] = []


class Smoke(Exception):
    """A failed expectation."""


def expect(condition, message):
    if not condition:
        raise Smoke(message)


def step(name):
    """Run a function as one reported step; a failure is recorded, not raised."""

    def wrap(fn):
        started = time.monotonic()
        try:
            fn()
        except Exception as exc:
            detail = (
                str(exc)
                if isinstance(exc, Smoke)
                else "".join(traceback.format_exception_only(type(exc), exc)).strip()
            )
            if not isinstance(exc, Smoke):
                traceback.print_exc()
            RESULTS.append((name, False, detail))
            print(f"FAIL  {name}: {detail}", flush=True)
        else:
            RESULTS.append((name, True, ""))
            print(f"PASS  {name} ({time.monotonic() - started:.1f}s)", flush=True)
        return fn

    return wrap


# ── environment ────────────────────────────────────────────────────────────────


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Project:
    """The demo project, pointed at throwaway state in ``tmp``."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        (tmp / "settings_wrapper").mkdir()
        (tmp / "settings_wrapper" / "smoke_agents_settings.py").write_text(
            "from config.settings import *  # noqa: F403\n"
            "VANTAGE = {**VANTAGE, 'AGENTS': True, 'AGENT_DEFAULT_ACCESS': 'write'}  # noqa: F405\n"
        )
        self.server: subprocess.Popen | None = None
        self.port = 0

    def env(self, agents=False):
        env = {
            **os.environ,
            "VANTAGE_DEMO_DB": str(self.tmp / "smoke.sqlite3"),
            "VANTAGE_DEMO_MEDIA": str(self.tmp / "media"),
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT / "src"), str(EXAMPLE), str(self.tmp / "settings_wrapper")]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "DJANGO_SETTINGS_MODULE": "smoke_agents_settings" if agents else "config.settings",
        }
        return env

    def manage(self, *args, agents=False, stdin=None, timeout=300):
        return subprocess.run(  # noqa: S603
            [sys.executable, str(EXAMPLE / "manage.py"), *args],
            cwd=EXAMPLE,
            env=self.env(agents),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start_server(self, agents=False):
        self.stop_server()
        self.port = free_port()
        self.server = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(EXAMPLE / "manage.py"),
                "runserver",
                f"127.0.0.1:{self.port}",
                "--noreload",
            ],
            cwd=EXAMPLE,
            env=self.env(agents),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise Smoke(f"runserver exited early with {self.server.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise Smoke("runserver did not start within 60s")

    def stop_server(self):
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        self.server = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"


# ── HTTP helpers ───────────────────────────────────────────────────────────────


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


class Client:
    """A tiny HTTP client. ``cookies=True`` keeps a session; ``token`` sends a bearer."""

    def __init__(self, base, *, cookies=True, token=None):
        self.base = base
        self.token = token
        handlers = (
            [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())] if cookies else []
        )
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, *, data=None, headers=None, json_body=None):
        headers = dict(headers or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)  # noqa: S310
        try:
            with self.opener.open(req, timeout=60) as resp:
                return Response(resp.status, resp.headers, resp.read(), resp.geturl())
        except urllib.error.HTTPError as err:
            return Response(err.code, err.headers, err.read(), err.geturl())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def cookie(self, name):
        for jar in (
            h for h in self.opener.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)
        ):
            for cookie in jar.cookiejar:
                if cookie.name == name:
                    return cookie.value
        return None

    def post_form(self, path, fields, *, referer=None):
        headers = {"Referer": self.base + (referer or path)}
        token = self.cookie("csrftoken")
        if token:
            fields = {**fields, "csrfmiddlewaretoken": token}
            headers["X-CSRFToken"] = token
        return self.request("POST", path, data=fields, headers=headers)


class FormParser(HTMLParser):
    """Collect the controls of every form on a page, with their rendered values."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"action": a.get("action", ""), "fields": {}, "selects": {}})
        elif not self.forms:
            return
        form = self.forms[-1] if self.forms else None
        if tag == "input" and a.get("name"):
            kind = (a.get("type") or "text").lower()
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    form["fields"][a["name"]] = a.get("value", "on")
            elif kind not in ("submit", "button", "file", "image"):
                form["fields"][a["name"]] = a.get("value", "")
        elif tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            form["fields"][self._textarea] = ""
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            form["selects"][self._select] = []
            if "multiple" not in a:  # an unselected multi-select submits nothing at all
                form["fields"].setdefault(self._select, "")
        elif tag == "option" and self._select:
            value = a.get("value", "")
            form["selects"][self._select].append(value)
            if "selected" in a:
                form["fields"][self._select] = value  # last selected wins; fine for a smoke

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._textarea and self.forms:
            self.forms[-1]["fields"][self._textarea] += data


def parse_forms(html):
    parser = FormParser()
    parser.feed(html)
    return parser.forms


def form_with(html, field):
    for form in parse_forms(html):
        if field in form["fields"]:
            return form
    raise Smoke(f"no form with a {field!r} control on the page")


# ── the walk ───────────────────────────────────────────────────────────────────


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vantage-smoke-"))
    project = Project(tmp)
    state: dict = {}
    try:
        phase_one(project, state)
        phase_two(project, state)
    except Smoke as exc:
        RESULTS.append(("setup", False, str(exc)))
        print(f"FAIL  setup: {exc}", flush=True)
    finally:
        project.stop_server()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


def phase_one(project: Project, state: dict):
    @step("manage.py migrate")
    def _():
        result = project.manage("migrate", "--noinput")
        expect(result.returncode == 0, result.stderr[-800:])

    @step("manage.py seed")
    def _():
        result = project.manage("seed")
        expect(result.returncode == 0, result.stderr[-800:])

    project.start_server()
    client = Client(project.base)
    state["client"] = client

    @step("sign in through the login form")
    def _():
        page = client.get("/accounts/login/")
        expect(page.status == 200, f"login page {page.status}")
        form = form_with(page.text, "username")
        fields = {**form["fields"], "username": "admin", "password": "admin12345"}
        done = client.post_form(
            form["action"] or "/accounts/login/", fields, referer="/accounts/login/"
        )
        expect(done.status == 200, f"login POST ended at {done.status}")
        expect("/login" not in done.url, f"still on the login page: {done.url}")
        expect(client.cookie("sessionid"), "no session cookie after login")

    def page(path, *needles, status=200):
        res = client.get(path)
        expect(res.status == status, f"{path} answered {res.status}")
        for needle in needles:
            expect(needle in res.text, f"{path} lacks {needle!r}")
        return res

    @step("dashboard renders")
    def _():
        page(f"{ADMIN}/", "Northwind Supply")

    @step("list page renders rows")
    def _():
        res = page(f"{ADMIN}/shop/product/", "NW-10")
        match = re.search(rf"{ADMIN}/shop/product/(\d+)/", res.text)
        expect(match, "no product link on the list")
        state["product_pk"] = match.group(1)
        record = client.get(f"{API}/shop/product/{match.group(1)}/").json()
        state["product_name"] = record["display"]

    @step("filters and search narrow the list")
    def _():
        everything = page(f"{ADMIN}/shop/order/").text
        hit = page(f"{ADMIN}/shop/product/?q={urllib.parse.quote(state['product_name'])}")
        expect(state["product_name"] in hit.text, "search lost the product it matched")
        miss = page(f"{ADMIN}/shop/product/?q=zz-no-such-product-zz")
        expect(
            state["product_name"] not in miss.text, "a search that matches nothing still lists it"
        )
        filtered = page(f"{ADMIN}/shop/order/?status=shipped")
        expect(filtered.text != everything, "status filter changed nothing")

    @step("detail page renders")
    def _():
        page(f"{ADMIN}/shop/product/{state['product_pk']}/", state["product_name"])

    @step("create a record through the form")
    def _():
        add = client.get(f"{ADMIN}/shop/product/add/")
        expect(add.status == 200, f"add page {add.status}")
        form = form_with(add.text, "sku")
        fields = dict(form["fields"])
        fields.update(
            name="Smoke Widget",
            sku="SMK-001",
            price="9.99",
            cost="4.00",
            stock="3",
            status="active",
            details_html="<p>Hello <strong>world</strong></p>",
        )
        for name, options in form["selects"].items():
            if name == "category" or (not fields.get(name) and name not in ("tags",)):
                fields[name] = next((o for o in options if o), "")
        done = client.post_form(
            form["action"] or f"{ADMIN}/shop/product/add/",
            fields,
            referer=f"{ADMIN}/shop/product/add/",
        )
        expect(done.status == 200, f"create answered {done.status}")
        listing = client.get(f"{ADMIN}/shop/product/?q=SMK-001&export=csv")
        expect("SMK-001" in listing.text, "created product not in the list")
        match = re.search(rf"{ADMIN}/shop/product/(\d+)/", done.url)
        if not match:
            found = client.get(f"{ADMIN}/shop/product/?q=SMK-001")
            match = re.search(rf"{ADMIN}/shop/product/(\d+)/", found.text)
        expect(match, f"cannot find the new product's id (ended at {done.url})")
        state["new_pk"] = match.group(1)

    @step("edit it, and the rich-text field is sanitised")
    def _():
        pk = state["new_pk"]
        edit_path = f"{ADMIN}/shop/product/{pk}/edit/"
        edit = client.get(edit_path)
        expect(edit.status == 200, f"edit page {edit.status}")
        form = form_with(edit.text, "details_html")
        fields = dict(form["fields"])
        fields["name"] = "Smoke Widget v2"
        fields["details_html"] = (
            '<p>Safe <em>text</em></p><script>alert("xss")</script>'
            '<img src=x onerror="alert(1)"><a href="javascript:alert(1)">x</a>'
        )
        done = client.post_form(form["action"] or edit_path, fields, referer=edit_path)
        expect(done.status == 200, f"edit answered {done.status}")
        again = client.get(edit_path).text
        expect("Smoke Widget v2" in again, "the edit was not saved")
        expect(
            "alert(&quot;xss&quot;)" not in again and 'alert("xss")' not in again,
            "script body survived on the edit page",
        )
        expect("<script>alert" not in again, "script tag survived on the edit page")
        expect("onerror" not in again.lower(), "event handler survived")
        expect("javascript:alert" not in again.lower(), "javascript: URL survived")
        expect("Safe" in again, "the harmless text was lost")
        stored = json.dumps(client.get(f"{API}/shop/product/{pk}/").json())
        expect(
            "<script" not in stored.lower() and "onerror" not in stored.lower(),
            "unsanitised markup in the stored value",
        )
        shown = client.get(f"{ADMIN}/shop/product/{pk}/").text
        expect(
            "<script>alert" not in shown and "onerror" not in shown.lower(),
            "unsanitised markup on the detail page",
        )

    @step("CSV export")
    def _():
        res = page(f"{ADMIN}/shop/product/?export=csv")
        expect("NW-1000" in res.text and "," in res.text, "csv has no rows")

    @step("JSON export")
    def _():
        res = page(f"{ADMIN}/shop/product/?export=json")
        rows = [json.loads(line) for line in res.text.splitlines() if line.strip()]
        expect(rows and "Sku" in rows[0], "json export has no rows")

    @step("Excel export (zip with xl/workbook.xml)")
    def _():
        res = page(f"{ADMIN}/shop/order/?export=xlsx")
        expect(res.body[:2] == b"PK", "xlsx does not start with PK")
        with zipfile.ZipFile(BytesIO(res.body)) as archive:
            expect("xl/workbook.xml" in archive.namelist(), "no xl/workbook.xml in the archive")

    @step("PDF export (%PDF magic)")
    def _():
        res = page(f"{ADMIN}/shop/order/?export=pdf")
        expect(res.body[:4] == b"%PDF", "pdf does not start with %PDF")

    @step("JSON API schema and a record read")
    def _():
        schema = client.get(f"{API}/schema/")
        expect(schema.status == 200, f"schema {schema.status}")
        expect("shop.product" in schema.text or "product" in schema.text, "product not in schema")
        record = client.get(f"{API}/shop/product/{state['product_pk']}/")
        expect(record.status == 200, f"record read {record.status}")
        expect(state["product_name"] in record.text, "record lacks the product name")

    @step("audit log shows the edits")
    def _():
        res = page(f"{ADMIN}/_/audit/")
        expect("Smoke Widget" in res.text, "audit log does not mention the edited product")

    @step("two-factor enrolment page loads")
    def _():
        page(f"{ADMIN}/_/account/two-factor/")


def phase_two(project: Project, state: dict):
    project.stop_server()
    project.start_server(agents=True)

    def create_agent(name, user, access):
        result = project.manage(
            "vantage_agent",
            "create",
            "--name",
            name,
            "--user",
            user,
            "--access",
            access,
            "--models",
            "shop.product",
            "shop.customer",
            agents=True,
        )
        expect(result.returncode == 0, result.stderr[-600:] or result.stdout[-600:])
        tokens = re.findall(r"vta_\S+", result.stdout)
        expect(tokens, "no token printed by vantage_agent create")
        return tokens[-1]

    @step("vantage_agent create (read and write identities)")
    def _():
        state["read_token"] = create_agent("smoke-reader", "svc-smoke", "read")
        state["write_token"] = create_agent("smoke-writer", "svc-smoke-w", "write")
        grant = (
            "from django.contrib.auth import get_user_model\n"
            "from django.contrib.auth.models import Group\n"
            "U = get_user_model()\n"
            "for n in ('svc-smoke', 'svc-smoke-w'):\n"
            "    u = U.objects.get(username=n)\n"
            "    for g in ('Catalogue editors', 'Support'):\n"
            "        u.groups.add(Group.objects.get(name=g))\n"
        )
        done = project.manage("shell", "-c", grant, agents=True)
        expect(done.returncode == 0, done.stderr[-600:])

    reader = Client(project.base, cookies=False)
    writer = Client(project.base, cookies=False)
    anon = Client(project.base, cookies=False)

    def tokens():
        reader.token, writer.token = state.get("read_token"), state.get("write_token")

    @step("agent API: read allowed, bad token refused")
    def _():
        tokens()
        res = reader.get(f"{API}/shop/product/")
        expect(res.status == 200, f"agent read {res.status}: {res.text[:200]}")
        anon.token = "vta_nope_" + "x" * 43
        expect(anon.get(f"{API}/shop/product/").status == 401, "a bad token was not a 401")

    @step("agent API: hidden field absent")
    def _():
        res = reader.get(f"{API}/shop/customer/")
        expect(res.status == 200, f"customer list {res.status}: {res.text[:200]}")
        expect("api_token" not in res.text, "the sensitive api_token field is visible to an agent")

    @step("agent API: write refused on a read agent")
    def _():
        pk = state["product_pk"]
        res = reader.request(
            "PATCH", f"{API}/shop/product/{pk}/", json_body={"fields": {"stock": 1}}
        )
        expect(res.status == 403, f"read agent write answered {res.status}")

    @step("agent API: dry run on a write agent changes nothing")
    def _():
        pk = state["product_pk"]
        before = writer.get(f"{API}/shop/product/{pk}/").json()
        res = writer.request(
            "PATCH", f"{API}/shop/product/{pk}/?dry_run=1", json_body={"fields": {"stock": 98765}}
        )
        expect(res.status == 200, f"dry run answered {res.status}: {res.text[:200]}")
        body = res.json()
        expect(body.get("dry_run") is True and body.get("valid") is True, f"odd dry run: {body}")
        after = writer.get(f"{API}/shop/product/{pk}/").json()
        expect(before == after, "a dry run changed the record")
        expect("98765" not in json.dumps(after), "dry-run value was saved")

    def rpc(client, method, params=None, ident=1):
        res = client.request(
            "POST",
            f"{ADMIN}/_/mcp/",
            json_body={"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        expect(res.status == 200, f"mcp {method} answered {res.status}: {res.text[:200]}")
        body = res.json()
        expect("error" not in body, f"mcp {method} error: {body.get('error')}")
        return body["result"]

    @step("MCP over HTTP: initialize, tools/list, tools/call")
    def _():
        info = rpc(
            reader,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        )
        expect("serverInfo" in info and "capabilities" in info, f"odd initialize: {info}")
        names = {t["name"] for t in rpc(reader, "tools/list", ident=2)["tools"]}
        expect("vantage_list_records" in names, f"tools: {sorted(names)}")
        expect("vantage_update_record" not in names, "a read agent was offered a write tool")
        called = rpc(
            reader,
            "tools/call",
            {
                "name": "vantage_list_records",
                "arguments": {"model": "shop.product", "page_size": 2},
            },
            ident=3,
        )
        expect(called.get("isError") is False, f"tool call failed: {called}")
        expect("NW-" in json.dumps(called), "tool result has no products")
        wnames = {t["name"] for t in rpc(writer, "tools/list", ident=4)["tools"]}
        expect("vantage_update_record" in wnames, "a write agent got no write tool")
        anon.token = None
        expect(
            anon.request("POST", f"{ADMIN}/_/mcp/", json_body={}).status == 401,
            "MCP without a token was not a 401",
        )

    @step("vantage_mcp over stdio: initialize")
    def _():
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "0"},
                    },
                }
            )
            + "\n"
        )
        done = project.manage(
            "vantage_mcp", "--agent", "smoke-reader", agents=True, stdin=request, timeout=120
        )
        expect(done.returncode == 0, done.stderr[-600:])
        first = next((ln for ln in done.stdout.splitlines() if ln.strip()), "")
        reply = json.loads(first)
        expect("serverInfo" in reply.get("result", {}), f"odd stdio reply: {first[:300]}")

    @step("manage.py check")
    def _():
        result = project.manage("check")
        expect(result.returncode == 0, (result.stdout + result.stderr)[-800:])

    @step("manage.py vantage_describe")
    def _():
        result = project.manage("vantage_describe")
        expect(result.returncode == 0 and result.stdout.strip(), result.stderr[-800:])

    @step("manage.py vantage_upgrade_check")
    def _():
        result = project.manage("vantage_upgrade_check")
        expect(result.returncode == 0, (result.stdout + result.stderr)[-800:])


if __name__ == "__main__":
    sys.exit(main())
