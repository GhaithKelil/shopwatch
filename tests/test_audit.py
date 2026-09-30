import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from database import init_db, upsert_orders_bulk, get_sync_stats, get_recent_orders
from loaders import CsvOrderLoader, ApiOrderLoader, parse_date
from models import Order, Alert
from rules import (
    rule_late_unshipped,
    rule_missing_tracking,
    rule_stuck_in_transit,
    rule_refund_spike,
    rule_fraud_velocity,
    rule_sync_stale,
    evaluate_rules,
)

def test_julianday_fulfillment():
    conn = sqlite3.connect(":memory:")
    res = conn.execute("SELECT julianday('2026-09-29T12:00:00+00:00') - julianday('2026-09-28T12:00:00+00:00')").fetchone()
    print("Julianday diff:", res[0])
    assert res[0] is not None
    assert round(res[0], 2) == 1.0

import concurrent.futures
from scheduler import run_sync_cycle

def test_concurrent_sync_cycles(tmp_path, monkeypatch):
    test_db = str(tmp_path / "concurrent.db")
    monkeypatch.setenv("DB_PATH", test_db)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(run_sync_cycle, db_path=test_db)
            for _ in range(4)
        ]
        results = [f.result() for f in futures]
    
    assert len(results) == 4
    for r in results:
        assert r.ok is True

    # Check for duplicate open alerts in DB
    conn = sqlite3.connect(test_db)
    cursor = conn.execute("SELECT rule_name, order_id, shop_name, count(*) as c FROM alerts WHERE is_resolved = 0 GROUP BY rule_name, order_id, shop_name HAVING c > 1")
    dups = cursor.fetchall()
    print("Duplicate open alerts found:", len(dups))
    assert len(dups) == 0, f"Found {len(dups)} duplicate alerts created by concurrent runs!"


def test_first_run_no_sync_stale_alert(tmp_path):
    test_db = str(tmp_path / "first_run.db")
    run_log = run_sync_cycle(db_path=test_db)
    assert run_log.ok is True

    # Confirm no sync_stale alert was generated on initial setup run
    conn = sqlite3.connect(test_db)
    stale_alerts = conn.execute("SELECT * FROM alerts WHERE rule_name = 'sync_stale'").fetchall()
    assert len(stale_alerts) == 0, "Initial run should not flag its own startup as stale"


def test_missing_tracking_respects_now():
    custom_now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    order = Order(
        receipt_id="NOW_TEST",
        shop_name="ExampleShop",
        created_at=custom_now - timedelta(days=2),
        paid_at=custom_now - timedelta(days=2),
        shipped_at=custom_now - timedelta(days=1),
        status="Shipped",
        is_paid=True,
        is_shipped=True,
        tracking_code=None,
    )
    alert = rule_missing_tracking(order, now=custom_now)
    assert alert is not None
    assert alert.created_at == custom_now


def test_alert_resolved_at_saved_in_model(tmp_path):
    test_db = str(tmp_path / "model_test.db")
    init_db(test_db)
    alert = Alert(
        order_id="RES_1",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Late order",
    )
    from database import insert_alert, resolve_alert, get_all_alerts
    aid = insert_alert(alert, test_db)
    assert aid > 0
    resolve_alert(aid, test_db)

    alerts = get_all_alerts(limit=10, db_path=test_db)
    assert len(alerts) == 1
    assert alerts[0].is_resolved is True
    assert alerts[0].resolved_at is not None
    assert isinstance(alerts[0].resolved_at, datetime)




