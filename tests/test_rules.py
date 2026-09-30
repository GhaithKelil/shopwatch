from datetime import datetime, timedelta, timezone
import pytest
from models import Order
from rules import (
    rule_late_unshipped,
    rule_missing_tracking,
    rule_stuck_in_transit,
    rule_refund_spike,
    rule_fraud_velocity,
    rule_sync_stale,
    evaluate_rules,
)


@pytest.fixture
def base_order():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    return Order(
        receipt_id="10001",
        shop_name="ExampleShop",
        source="csv",
        created_at=now - timedelta(days=1),
        paid_at=now - timedelta(days=1),
        shipped_at=None,
        status="Paid",
        total_amount=45.0,
        currency="EUR",
        country="Germany",
        buyer_name="John Doe",
        buyer_id="user_123",
        shipping_address="123 Test St, Berlin, 10115, Germany",
        tracking_code=None,
        is_paid=True,
        is_shipped=False,
    )


def test_late_unshipped_within_threshold(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    # Order paid 1 day ago, threshold is 3 days
    alert = rule_late_unshipped(base_order, now=now, threshold_days=3)
    assert alert is None


def test_late_unshipped_exceeds_threshold(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.paid_at = now - timedelta(days=4)
    alert = rule_late_unshipped(base_order, now=now, threshold_days=3)

    assert alert is not None
    assert alert.rule_name == "late_unshipped"
    assert alert.severity == "CRITICAL"
    assert "remained unshipped" in alert.message
    assert alert.order_id == "10001"


def test_late_unshipped_already_shipped(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.paid_at = now - timedelta(days=5)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=4)

    assert rule_late_unshipped(base_order, now=now) is None


def test_late_unshipped_canceled_order(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.paid_at = now - timedelta(days=5)
    base_order.status = "Canceled"

    assert rule_late_unshipped(base_order, now=now) is None


def test_missing_tracking_unshipped(base_order):
    # Unshipped order should not trigger missing tracking alert
    assert rule_missing_tracking(base_order) is None


def test_missing_tracking_shipped_without_code(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=1)
    base_order.tracking_code = ""

    alert = rule_missing_tracking(base_order)
    assert alert is not None
    assert alert.rule_name == "missing_tracking"
    assert alert.severity == "WARNING"
    assert alert.order_id == "10001"


def test_missing_tracking_shipped_with_code(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=1)
    base_order.tracking_code = "CY123456789DE"

    assert rule_missing_tracking(base_order) is None


def test_stuck_in_transit_within_threshold(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=5)

    assert rule_stuck_in_transit(base_order, now=now, threshold_days=14) is None


def test_stuck_in_transit_exceeds_threshold(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=18)
    base_order.status = "Completed"

    alert = rule_stuck_in_transit(base_order, now=now, threshold_days=14)
    assert alert is not None
    assert alert.rule_name == "stuck_in_transit"
    assert alert.severity == "WARNING"
    assert "has not reached delivered status" in alert.message


def test_stuck_in_transit_delivered_order(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=20)
    base_order.status = "Delivered"

    assert rule_stuck_in_transit(base_order, now=now) is None


def test_refund_spike_under_threshold():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = [
        Order(
            receipt_id=f"200{i}",
            shop_name="ExampleShop",
            created_at=now - timedelta(days=i),
            status="Completed" if i != 1 else "Refunded",
            total_amount=20.0,
        )
        for i in range(10)
    ]
    # 1 out of 10 = 10.0%, threshold is 15.0%
    alerts = rule_refund_spike(orders, now=now, threshold_pct=15.0, min_orders=5)
    assert len(alerts) == 0


def test_refund_spike_exceeds_threshold():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = []
    for i in range(10):
        # 3 refunds out of 10 = 30% > 10%
        st = "Refunded" if i < 3 else "Completed"
        orders.append(
            Order(
                receipt_id=f"200{i}",
                shop_name="ExampleShop",
                created_at=now - timedelta(days=i),
                status=st,
                total_amount=25.0,
            )
        )

    alerts = rule_refund_spike(orders, now=now, threshold_pct=10.0, min_orders=5)
    assert len(alerts) == 1
    assert alerts[0].rule_name == "refund_spike"
    assert alerts[0].severity == "CRITICAL"
    assert "30.0%" in alerts[0].message


def test_fraud_velocity():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = [
        Order(
            receipt_id="3001",
            shop_name="ExampleShop",
            created_at=now - timedelta(hours=2),
            buyer_id="buyer_fast",
            buyer_name="Fast Buyer",
        ),
        Order(
            receipt_id="3002",
            shop_name="ExampleShop",
            created_at=now - timedelta(hours=5),
            buyer_id="buyer_fast",
            buyer_name="Fast Buyer",
        ),
        Order(
            receipt_id="3003",
            shop_name="ExampleShop",
            created_at=now - timedelta(hours=10),
            buyer_id="buyer_fast",
            buyer_name="Fast Buyer",
        ),
        Order(
            receipt_id="3004",
            shop_name="ExampleShop",
            created_at=now - timedelta(days=3),  # outside 24h window
            buyer_id="buyer_fast",
            buyer_name="Fast Buyer",
        ),
    ]

    alerts = rule_fraud_velocity(orders, now=now, window_hours=24, max_orders=3)
    assert len(alerts) == 1
    assert alerts[0].rule_name == "buyer_velocity"
    assert "3 orders placed within 24 hours" in alerts[0].message


def test_sync_stale_healthy():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(minutes=30)
    assert rule_sync_stale(last_run, now=now, max_stale_hours=2.0) is None


def test_sync_stale_breached():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(hours=3)
    alert = rule_sync_stale(last_run, now=now, max_stale_hours=2.0)

    assert alert is not None
    assert alert.rule_name == "sync_stale"
    assert alert.severity == "CRITICAL"
    assert "3.0 hours ago" in alert.message


def test_sync_stale_never_run():
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    alert = rule_sync_stale(None, now=now)
    assert alert is not None
    assert "never completed" in alert.message


def test_evaluate_rules_combined(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.paid_at = now - timedelta(days=5)  # triggers late unshipped

    last_run = now - timedelta(hours=4)  # triggers sync stale
    alerts = evaluate_rules([base_order], last_successful_run=last_run, now=now)

    rule_names = {a.rule_name for a in alerts}
    assert "late_unshipped" in rule_names
    assert "sync_stale" in rule_names


def test_stuck_in_transit_ignored_when_tracked(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=30)
    base_order.status = "Completed"
    base_order.tracking_code = "LI395697962LT"

    assert rule_stuck_in_transit(base_order, now=now) is None


def test_stuck_in_transit_untracked_flagged(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.is_shipped = True
    base_order.shipped_at = now - timedelta(days=30)
    base_order.status = "Completed"

    assert rule_stuck_in_transit(base_order, now=now) is not None


def test_fully_refunded_order_is_ignored_by_shipping_rules(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    base_order.status = "Fully Refunded"
    base_order.paid_at = now - timedelta(days=10)

    assert rule_late_unshipped(base_order, now=now) is None


def test_refund_spike_counts_fully_refunded(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = []
    for i in range(5):
        o = base_order.model_copy(update={"receipt_id": str(i)})
        o.status = "Fully Refunded" if i < 2 else "Completed"
        orders.append(o)

    assert len(rule_refund_spike(orders, now=now)) == 1


def test_velocity_alert_uses_numerically_latest_order(base_order):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    orders = [
        base_order.model_copy(update={"receipt_id": rid, "created_at": now - timedelta(hours=1)})
        for rid in ("9", "10", "11")
    ]

    alerts = rule_fraud_velocity(orders, now=now)
    assert alerts[0].order_id == "11"
