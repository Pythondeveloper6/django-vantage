"""xlsx and pdf carry the same cells as CSV where masking, field permissions and tenant
scoping decide what a user may see."""

from django.contrib.auth.models import User
from django.urls import reverse

from tests.test_export import body_of, read, sheet_cells
from tests.test_field_permissions import (  # noqa: F401
    ORDER_LIST,
    agent,
    as_agent,
    order,
    order_panel,
    support,
)
from tests.test_tenancy import client_as, http_tenancy, list_url, orgs, tenants  # noqa: F401
from tests.testapp.models import Author, TntWidget, ZCredential
from vantage import export, site

TOKEN = "sk_live_TOKENVALUE0123456789"


def test_masked_sensitive_columns(client, db, monkeypatch):
    owner = Author.objects.create(name="Ada", email="ada@example.com")
    ZCredential.objects.create(name="Production key", owner=owner, api_token=TOKEN)
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    monkeypatch.setattr(site.get_panel(ZCredential), "list_display", ["name", "api_token"])
    url = reverse("vantage:testapp-zcredential-list")
    csv_rows = read(client.get(url, {"export": "csv"}))
    assert "TOKENVALUE" not in str(csv_rows)
    assert sheet_cells(body_of(client.get(url, {"export": "xlsx"}))) == csv_rows
    pdf = body_of(client.get(url, {"export": "pdf"}))
    assert b"TOKENVALUE" not in pdf
    for cell in csv_rows[1]:
        assert export._pdf_text(cell) in pdf


def test_a_tenant_scoped_queryset(db, http_tenancy, tenants):  # noqa: F811
    def get(fmt):
        return client_as(tenants.alice).get(list_url(TntWidget), {"export": fmt})

    csv_rows = read(get("csv"))
    assert sheet_cells(body_of(get("xlsx"))) == csv_rows
    pdf = body_of(get("pdf"))
    assert b"Widget A" in pdf and b"Widget B" not in pdf


def test_a_field_the_user_may_not_see(as_agent, order, monkeypatch):  # noqa: F811
    monkeypatch.setattr(order_panel(), "list_display", ["number", "total"])
    url = reverse(ORDER_LIST)
    csv_rows = read(as_agent.get(url, {"export": "csv"}))
    assert sheet_cells(body_of(as_agent.get(url, {"export": "xlsx"}))) == csv_rows
    pdf = body_of(as_agent.get(url, {"export": "pdf"}))
    assert b"A-1001" in pdf and b"4321" not in pdf and b"(Total)" not in pdf
    assert b"4321" not in body_of(as_agent.get(url, {"export": "xlsx"}))
