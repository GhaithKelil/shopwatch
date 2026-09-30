from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app import app
from database import init_db, upsert_order, insert_alert
from models import Order, Alert


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "api_test.db")
    monkeypatch.setenv("DB_PATH", test_db)
    init_db(test_db)
    yield test_db


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database_connected"] is True
    assert "total_orders" in data
    assert "open_alerts" in data


def test_metrics_and_stats(client, setup_test_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    order = Order(
        receipt_id="API_101",
        shop_name="ExampleShop",
        created_at=now,
        total_amount=50.0,
        currency="EUR",
    )
    upsert_order(order, setup_test_db)

    resp1 = client.get("/api/stats")
    assert resp1.status_code == 200
    data = resp1.json()
    assert data["total_orders"] == 1
    assert data["orders_by_shop"]["ExampleShop"] == 1

    resp2 = client.get("/metrics")
    assert resp2.status_code == 200
    assert resp2.json()["total_orders"] == 1


def test_orders_filtering_and_pagination(client, setup_test_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        order = Order(
            receipt_id=f"ORDER_{i}",
            shop_name="ExampleShop" if i < 3 else "SecondaryShop",
            buyer_name=f"Customer {i}",
            created_at=now,
            total_amount=20.0 * (i + 1),
            currency="EUR",
        )
        upsert_order(order, setup_test_db)

    # All orders
    res = client.get("/api/orders")
    assert res.status_code == 200
    assert res.json()["total"] == 5
    assert len(res.json()["orders"]) == 5

    # Filter by shop
    res_example = client.get("/api/orders?shop=ExampleShop")
    assert res_example.json()["total"] == 3

    # Search
    res_search = client.get("/api/orders?search=Customer 4")
    assert res_search.json()["total"] == 1
    assert res_search.json()["orders"][0]["receipt_id"] == "ORDER_4"


def test_alerts_and_resolve(client, setup_test_db):
    alert = Alert(
        order_id="TEST_ORD_1",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Order is overdue for dispatch",
    )
    alert_id = insert_alert(alert, setup_test_db)

    # Fetch alerts
    res = client.get("/api/alerts")
    assert res.status_code == 200
    alerts = res.json()
    assert len(alerts) == 1
    assert alerts[0]["id"] == alert_id
    assert alerts[0]["rule_name"] == "late_unshipped"

    # Resolve alert
    res_resolve = client.post(f"/api/alerts/{alert_id}/resolve")
    assert res_resolve.status_code == 200
    assert res_resolve.json()["resolved"] is True

    # Now open alerts should be empty
    res_open = client.get("/api/alerts")
    assert len(res_open.json()) == 0

    # Resolving non-existent or already resolved alert returns 404
    res_404 = client.post(f"/api/alerts/99999/resolve")
    assert res_404.status_code == 404


def test_dashboard_and_docs_html(client):
    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "ShopWatch" in res_dash.text
    assert "text/html" in res_dash.headers["content-type"]

    res_docs = client.get("/docs")
    assert res_docs.status_code == 200
    assert "Swagger" in res_docs.text or "swagger-ui" in res_docs.text


def test_trigger_sync_endpoint(client):
    res = client.post("/api/sync")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "run" in data
    assert data["run"]["ok"] is True


def test_resolve_all_alerts_endpoint(client, setup_test_db):
    a1 = Alert(order_id="BATCH_1", shop_name="ExampleShop", rule_name="late_unshipped", severity="CRITICAL", message="Critical late")
    a2 = Alert(order_id="BATCH_2", shop_name="ExampleShop", rule_name="missing_tracking", severity="WARNING", message="Warning track")
    a3 = Alert(order_id="BATCH_3", shop_name="ExampleShop", rule_name="missing_tracking", severity="WARNING", message="Warning track 2")
    insert_alert(a1, setup_test_db)
    insert_alert(a2, setup_test_db)
    insert_alert(a3, setup_test_db)

    # Filtered batch resolve by severity
    res_warn = client.post("/api/alerts/resolve-all?severity=WARNING")
    assert res_warn.status_code == 200
    assert res_warn.json()["resolved_count"] == 2

    # Remaining open alerts
    res_open = client.get("/api/alerts")
    assert len(res_open.json()) == 1
    assert res_open.json()[0]["severity"] == "CRITICAL"

    # Resolve all remaining
    res_all = client.post("/api/alerts/resolve-all")
    assert res_all.status_code == 200
    assert res_all.json()["resolved_count"] == 1
    assert len(client.get("/api/alerts").json()) == 0


def test_dashboard_elements(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert "shop-names" in html
    assert "system-status-indicator" in html
    assert "order-modal" in html
    assert "tab-warnings" in html
    assert "tab-transit" in html
    assert "tab-unshipped" in html
    assert "resolveAllVisibleAlerts" in html
    assert "openOrderModal" in html

