from datetime import datetime, timezone, timedelta
import pytest
from models import Order, Alert, RunLog
from database import (
    init_db,
    upsert_order,
    upsert_orders_bulk,
    get_all_orders_for_rules,
    get_recent_orders,
    get_order_count,
    insert_alert,
    is_alert_open,
    get_open_alerts,
    mark_alert_sent,
    resolve_alert,
    record_run,
    get_last_run,
    get_last_successful_run,
    get_sync_stats,
    auto_resolve_cleared_alerts,
    resolve_sync_stale_alerts,
    resolve_all_alerts,
)



@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    init_db(db_file)
    return db_file


def test_upsert_order_and_idempotency(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    order = Order(
        receipt_id="99001",
        shop_name="ExampleShop",
        source="csv",
        created_at=now,
        status="Paid",
        total_amount=29.99,
        currency="EUR",
    )

    upsert_order(order, temp_db)
    orders = get_all_orders_for_rules(temp_db)
    assert len(orders) == 1
    assert orders[0].receipt_id == "99001"
    assert orders[0].total_amount == 29.99

    # Update order with new status and amount - should update, not create duplicate
    order.status = "Completed"
    order.total_amount = 35.00
    order.is_shipped = True
    order.shipped_at = now + timedelta(days=1)
    upsert_order(order, temp_db)

    orders = get_all_orders_for_rules(temp_db)
    assert len(orders) == 1
    assert orders[0].status == "Completed"
    assert orders[0].total_amount == 35.00
    assert orders[0].is_shipped is True


def test_bulk_upsert_and_pagination(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = [
        Order(
            receipt_id=f"bulk_{i}",
            shop_name="ExampleShop" if i % 2 == 0 else "SecondaryShop",
            source="csv",
            created_at=now - timedelta(hours=i),
            status="Paid" if i % 3 == 0 else "Completed",
            total_amount=10.0 + i,
            currency="EUR",
        )
        for i in range(25)
    ]
    count = upsert_orders_bulk(orders, temp_db)
    assert count == 25

    # Test pagination
    page1 = get_recent_orders(limit=10, offset=0, db_path=temp_db)
    assert len(page1) == 10
    page2 = get_recent_orders(limit=10, offset=10, db_path=temp_db)
    assert len(page2) == 10
    page3 = get_recent_orders(limit=10, offset=20, db_path=temp_db)
    assert len(page3) == 5

    # Filter by shop
    example_orders = get_recent_orders(shop_name="ExampleShop", db_path=temp_db)
    assert len(example_orders) == 13
    assert get_order_count(shop_name="ExampleShop", db_path=temp_db) == 13

    # Filter by status
    paid_count = get_order_count(status="Paid", db_path=temp_db)
    assert paid_count == 9


def test_alert_lifecycle_and_deduplication(temp_db):
    alert = Alert(
        order_id="99002",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Unshipped order alert",
    )

    assert not is_alert_open("late_unshipped", "99002", "ExampleShop", temp_db)
    alert_id = insert_alert(alert, temp_db)
    assert alert_id > 0
    assert is_alert_open("late_unshipped", "99002", "ExampleShop", temp_db)

    open_alerts = get_open_alerts(temp_db)
    assert len(open_alerts) == 1
    assert open_alerts[0].sent_to_telegram is False

    mark_alert_sent(alert_id, temp_db)
    open_alerts = get_open_alerts(temp_db)
    assert open_alerts[0].sent_to_telegram is True

    resolved = resolve_alert(alert_id, temp_db)
    assert resolved is True
    assert not is_alert_open("late_unshipped", "99002", "ExampleShop", temp_db)
    assert len(get_open_alerts(temp_db)) == 0


def test_run_logs_and_stats(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    run1 = RunLog(
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2, seconds=-5),
        ok=True,
        orders_fetched=10,
        alerts_generated=1,
    )
    record_run(run1, temp_db)

    run2 = RunLog(
        started_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=10, seconds=-5),
        ok=False,
        error_message="Connection timeout",
    )
    record_run(run2, temp_db)

    last_run = get_last_run(temp_db)
    assert last_run is not None
    assert last_run.ok is False
    assert last_run.error_message == "Connection timeout"

    last_success = get_last_successful_run(temp_db)
    assert last_success is not None
    assert last_success.ok is True
    assert last_success.orders_fetched == 10

    stats = get_sync_stats(temp_db)
    assert stats["last_run"]["ok"] is False


def test_auto_resolve_cleared_alerts(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    # Order 1 is initially unshipped and triggers late_unshipped alert
    order1 = Order(
        receipt_id="AUTO_1",
        shop_name="ExampleShop",
        created_at=now - timedelta(days=5),
        paid_at=now - timedelta(days=5),
        status="Paid",
        is_paid=True,
        is_shipped=False,
    )
    upsert_order(order1, temp_db)
    insert_alert(
        Alert(
            order_id="AUTO_1",
            shop_name="ExampleShop",
            rule_name="late_unshipped",
            severity="CRITICAL",
            message="Late unshipped",
        ),
        temp_db,
    )

    # Order 2 has missing tracking alert
    order2 = Order(
        receipt_id="AUTO_2",
        shop_name="ExampleShop",
        created_at=now - timedelta(days=2),
        paid_at=now - timedelta(days=2),
        shipped_at=now - timedelta(days=1),
        status="Shipped",
        is_paid=True,
        is_shipped=True,
        tracking_code="",
    )
    upsert_order(order2, temp_db)
    insert_alert(
        Alert(
            order_id="AUTO_2",
            shop_name="ExampleShop",
            rule_name="missing_tracking",
            severity="WARNING",
            message="Missing tracking",
        ),
        temp_db,
    )

    assert len(get_open_alerts(temp_db)) == 2

    # Seller fulfills Order 1 (marks it shipped)
    order1.is_shipped = True
    order1.shipped_at = now
    upsert_order(order1, temp_db)

    # Seller adds tracking to Order 2
    order2.tracking_code = "TRACK123"
    upsert_order(order2, temp_db)

    # Run auto_resolve_cleared_alerts
    resolved_count = auto_resolve_cleared_alerts(temp_db)
    assert resolved_count == 2
    assert len(get_open_alerts(temp_db)) == 0


def test_atomic_duplicate_alert_ignore(temp_db):
    alert = Alert(
        order_id="ATOM_1",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Critical late",
    )
    id1 = insert_alert(alert, temp_db)
    assert id1 > 0

    # Attempt inserting identical open alert - must be safely ignored by unique index
    id2 = insert_alert(alert, temp_db)
    assert id2 == 0
    assert len(get_open_alerts(temp_db)) == 1


def test_search_by_tracking_code(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    order = Order(
        receipt_id="TRACK_ORD_1",
        shop_name="ExampleShop",
        created_at=now,
        status="Completed",
        tracking_code="CY998877665DE",
    )
    upsert_order(order, temp_db)

    found = get_recent_orders(search="CY998877665DE", db_path=temp_db)
    assert len(found) == 1
    assert found[0].receipt_id == "TRACK_ORD_1"
    assert get_order_count(search="CY998877665DE", db_path=temp_db) == 1


def test_resolve_all_alerts_db(temp_db):
    a1 = Alert(order_id="ALL_1", shop_name="ExampleShop", rule_name="late_unshipped", severity="CRITICAL", message="M1")
    a2 = Alert(order_id="ALL_2", shop_name="ExampleShop", rule_name="missing_tracking", severity="WARNING", message="M2")
    a3 = Alert(order_id="ALL_3", shop_name="ExampleShop", rule_name="missing_tracking", severity="WARNING", message="M3")
    insert_alert(a1, temp_db)
    insert_alert(a2, temp_db)
    insert_alert(a3, temp_db)

    assert len(get_open_alerts(temp_db)) == 3

    # Resolve WARNING only
    resolved_warn = resolve_all_alerts(severity="WARNING", db_path=temp_db)
    assert resolved_warn == 2
    assert len(get_open_alerts(temp_db)) == 1

    # Resolve all remaining
    resolved_remaining = resolve_all_alerts(db_path=temp_db)
    assert resolved_remaining == 1
    assert len(get_open_alerts(temp_db)) == 0




def test_auto_resolve_is_scoped_to_shop(temp_db):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    shipped = Order(receipt_id="1001", shop_name="B", source="csv", created_at=now,
                    shipped_at=now, status="Completed", total_amount=1, currency="EUR",
                    is_paid=True, is_shipped=True)
    unshipped = Order(receipt_id="1001", shop_name="A", source="csv", created_at=now,
                      status="Paid", total_amount=1, currency="EUR", is_paid=True)
    upsert_orders_bulk([shipped, unshipped], temp_db)
    insert_alert(Alert(order_id="1001", shop_name="A", rule_name="late_unshipped",
                       severity="CRITICAL", message="late", created_at=now), temp_db)

    assert auto_resolve_cleared_alerts(temp_db) == 0
    assert len(get_open_alerts(temp_db)) == 1


def test_cleared_aggregate_alerts_are_resolved(temp_db):
    from database import resolve_cleared_aggregate_alerts
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    insert_alert(Alert(order_id=None, shop_name="A", rule_name="refund_spike",
                       severity="CRITICAL", message="spike", created_at=now), temp_db)

    assert resolve_cleared_aggregate_alerts(set(), temp_db) == 1
    assert get_open_alerts(temp_db) == []
    insert_alert(Alert(order_id=None, shop_name="A", rule_name="refund_spike",
                       severity="CRITICAL", message="spike again", created_at=now), temp_db)
    assert resolve_cleared_aggregate_alerts({("refund_spike", None, "A")}, temp_db) == 0
