import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List, Optional, Tuple, Dict, Any

from models import Order, Alert, RunLog, utc_now

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/shopwatch.db")

_initialized: set = set()
_init_lock = threading.Lock()


def get_db_path(db_path: Optional[str] = None) -> str:
    """Resolve and ensure the directory for the database path exists."""
    resolved = db_path or os.environ.get("DB_PATH") or DEFAULT_DB_PATH
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


@contextmanager
def get_connection(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional SQLite connection with busy timeout and WAL support."""
    path = get_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite schema and persistent WAL mode if it doesn't already exist."""
    path = get_db_path(db_path)
    with _init_lock:
        if path in _initialized and os.path.exists(path):
            return
        _init_schema(path, db_path)
        _initialized.add(path)


def _set_wal_mode(path: str) -> None:
    """Switch the file to WAL once. Switching needs an exclusive lock, so retry if another process holds it."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        for attempt in range(40):
            try:
                if conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL;")
                return
            except sqlite3.OperationalError:
                if attempt == 39:
                    raise
                time.sleep(0.25)
    finally:
        conn.close()


def _init_schema(path: str, db_path: Optional[str]) -> None:
    _set_wal_mode(path)

    with get_connection(db_path) as conn:
        conn.executescript("""

            CREATE TABLE IF NOT EXISTS orders (
                receipt_id TEXT NOT NULL,
                shop_name TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                shipped_at TEXT,
                status TEXT NOT NULL,
                total_amount REAL NOT NULL,
                currency TEXT NOT NULL,
                country TEXT,
                buyer_name TEXT,
                buyer_id TEXT,
                shipping_address TEXT,
                tracking_code TEXT,
                is_paid INTEGER NOT NULL,
                is_shipped INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (receipt_id, shop_name)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_shop ON orders(shop_name);
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_orders_buyer_id ON orders(buyer_id);

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                shop_name TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_resolved INTEGER NOT NULL DEFAULT 0,
                sent_to_telegram INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(is_resolved, rule_name);
            CREATE INDEX IF NOT EXISTS idx_alerts_order ON alerts(order_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_unique_open_order
            ON alerts(rule_name, order_id, shop_name)
            WHERE is_resolved = 0 AND order_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_unique_open_shop
            ON alerts(rule_name, shop_name)
            WHERE is_resolved = 0 AND order_id IS NULL;

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                ok INTEGER NOT NULL,
                orders_fetched INTEGER NOT NULL DEFAULT 0,
                alerts_generated INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
        """)


def _order_to_tuple(order: Order, updated_at: str) -> Tuple:
    return (
        order.receipt_id,
        order.shop_name,
        order.source,
        order.created_at.isoformat(),
        order.paid_at.isoformat() if order.paid_at else None,
        order.shipped_at.isoformat() if order.shipped_at else None,
        order.status,
        order.total_amount,
        order.currency,
        order.country,
        order.buyer_name,
        order.buyer_id,
        order.shipping_address,
        order.tracking_code,
        1 if order.is_paid else 0,
        1 if order.is_shipped else 0,
        updated_at,
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        receipt_id=row["receipt_id"],
        shop_name=row["shop_name"],
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
        paid_at=datetime.fromisoformat(row["paid_at"]) if row["paid_at"] else None,
        shipped_at=datetime.fromisoformat(row["shipped_at"]) if row["shipped_at"] else None,
        status=row["status"],
        total_amount=float(row["total_amount"]),
        currency=row["currency"],
        country=row["country"],
        buyer_name=row["buyer_name"],
        buyer_id=row["buyer_id"],
        shipping_address=row["shipping_address"],
        tracking_code=row["tracking_code"],
        is_paid=bool(row["is_paid"]),
        is_shipped=bool(row["is_shipped"]),
    )


def _row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        id=row["id"],
        order_id=row["order_id"],
        shop_name=row["shop_name"],
        rule_name=row["rule_name"],
        severity=row["severity"],
        message=row["message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        is_resolved=bool(row["is_resolved"]),
        sent_to_telegram=bool(row["sent_to_telegram"]),
        resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
    )



def upsert_order(order: Order, db_path: Optional[str] = None) -> None:
    """Insert or update a single normalized order idempotently."""
    upsert_orders_bulk([order], db_path)


def upsert_orders_bulk(orders: List[Order], db_path: Optional[str] = None) -> int:
    """Insert or update orders idempotently in bulk within a single transaction."""
    if not orders:
        return 0

    init_db(db_path)
    now_str = utc_now().isoformat()
    rows = [_order_to_tuple(o, now_str) for o in orders]

    query = """
        INSERT INTO orders (
            receipt_id, shop_name, source, created_at, paid_at, shipped_at,
            status, total_amount, currency, country, buyer_name, buyer_id,
            shipping_address, tracking_code, is_paid, is_shipped, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(receipt_id, shop_name) DO UPDATE SET
            source=excluded.source,
            created_at=excluded.created_at,
            paid_at=excluded.paid_at,
            shipped_at=excluded.shipped_at,
            status=excluded.status,
            total_amount=excluded.total_amount,
            currency=excluded.currency,
            country=excluded.country,
            buyer_name=excluded.buyer_name,
            buyer_id=excluded.buyer_id,
            shipping_address=excluded.shipping_address,
            tracking_code=excluded.tracking_code,
            is_paid=excluded.is_paid,
            is_shipped=excluded.is_shipped,
            updated_at=excluded.updated_at
    """
    with get_connection(db_path) as conn:
        conn.executemany(query, rows)
    return len(orders)


def get_all_orders_for_rules(db_path: Optional[str] = None) -> List[Order]:
    """Retrieve all orders from DB for rule evaluation."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT * FROM orders ORDER BY created_at DESC")
        return [_row_to_order(row) for row in cursor.fetchall()]


def get_recent_orders(
    limit: int = 100,
    offset: int = 0,
    shop_name: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Order]:
    """Retrieve paginated and filtered orders."""
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if shop_name:
        conditions.append("shop_name = ?")
        params.append(shop_name)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if search:
        s = f"%{search.strip()}%"
        conditions.append("(receipt_id LIKE ? OR buyer_name LIKE ? OR buyer_id LIKE ? OR tracking_code LIKE ?)")
        params.extend([s, s, s, s])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM orders {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection(db_path) as conn:
        cursor = conn.execute(query, params)
        return [_row_to_order(row) for row in cursor.fetchall()]


def get_order_count(
    shop_name: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Count total orders matching the filter criteria."""
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if shop_name:
        conditions.append("shop_name = ?")
        params.append(shop_name)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if search:
        s = f"%{search.strip()}%"
        conditions.append("(receipt_id LIKE ? OR buyer_name LIKE ? OR buyer_id LIKE ? OR tracking_code LIKE ?)")
        params.extend([s, s, s, s])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT COUNT(*) as count FROM orders {where_clause}"

    with get_connection(db_path) as conn:
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        return row["count"] if row else 0


def insert_alert(alert: Alert, db_path: Optional[str] = None) -> int:
    """Save a new alert to SQLite and return the generated ID, or 0 if duplicate ignored."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO alerts (order_id, shop_name, rule_name, severity, message, created_at, is_resolved, sent_to_telegram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.order_id,
                alert.shop_name,
                alert.rule_name,
                alert.severity,
                alert.message,
                alert.created_at.isoformat(),
                1 if alert.is_resolved else 0,
                1 if alert.sent_to_telegram else 0,
            ),
        )
        if cursor.rowcount == 0:
            return 0
        return cursor.lastrowid or 0


def is_alert_open(
    rule_name: str,
    order_id: Optional[str],
    shop_name: str,
    db_path: Optional[str] = None,
) -> bool:
    """Check if an unresolved alert for the given rule and order already exists."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        if order_id:
            cursor = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE rule_name = ? AND order_id = ? AND shop_name = ? AND is_resolved = 0
                LIMIT 1
                """,
                (rule_name, str(order_id), shop_name),
            )
        else:
            cursor = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE rule_name = ? AND shop_name = ? AND is_resolved = 0
                LIMIT 1
                """,
                (rule_name, shop_name),
            )
        return cursor.fetchone() is not None



def get_open_alerts(db_path: Optional[str] = None) -> List[Alert]:
    """Retrieve all open (unresolved) alerts."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM alerts WHERE is_resolved = 0 ORDER BY created_at DESC"
        )
        return [_row_to_alert(row) for row in cursor.fetchall()]


def get_all_alerts(limit: int = 100, db_path: Optional[str] = None) -> List[Alert]:
    """Retrieve recent alerts up to limit."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_alert(row) for row in cursor.fetchall()]


def mark_alert_sent(alert_id: int, db_path: Optional[str] = None) -> None:
    """Mark an alert as dispatched to Telegram."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET sent_to_telegram = 1 WHERE id = ?",
            (alert_id,),
        )


def resolve_alert(alert_id: int, db_path: Optional[str] = None) -> bool:
    """Mark an alert as resolved."""
    init_db(db_path)
    now_str = utc_now().isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET is_resolved = 1, resolved_at = ? WHERE id = ?",
            (now_str, alert_id),
        )
        return cursor.rowcount > 0


def resolve_all_alerts(severity: Optional[str] = None, db_path: Optional[str] = None) -> int:
    """Mark open alerts as resolved, optionally filtered by severity."""
    init_db(db_path)
    now_str = utc_now().isoformat()
    with get_connection(db_path) as conn:
        if severity:
            cursor = conn.execute(
                "UPDATE alerts SET is_resolved = 1, resolved_at = ? WHERE is_resolved = 0 AND severity = ?",
                (now_str, severity),
            )
        else:
            cursor = conn.execute(
                "UPDATE alerts SET is_resolved = 1, resolved_at = ? WHERE is_resolved = 0",
                (now_str,),
            )
        return cursor.rowcount



def auto_resolve_cleared_alerts(db_path: Optional[str] = None) -> int:
    """
    Reconcile open alerts against current database state.
    Auto-resolves alerts whose offending conditions have been resolved
    (e.g., late order was shipped, tracking code was added, order was delivered/canceled).
    Returns count of alerts auto-resolved.
    """
    init_db(db_path)
    now_str = utc_now().isoformat()
    resolved_count = 0
    with get_connection(db_path) as conn:
        # 1. Resolve late_unshipped if order is now shipped or canceled/refunded
        c1 = conn.execute(
            """
            UPDATE alerts SET is_resolved = 1, resolved_at = ?
            WHERE is_resolved = 0 AND rule_name = 'late_unshipped'
              AND EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.receipt_id = alerts.order_id AND o.shop_name = alerts.shop_name
                    AND (o.is_shipped = 1 OR o.shipped_at IS NOT NULL
                     OR LOWER(o.status) IN ('canceled', 'refunded', 'fully refunded', 'cancelled'))
              )
            """,
            (now_str,),
        )
        resolved_count += c1.rowcount

        # 2. Resolve missing_tracking if tracking code was added or order canceled
        c2 = conn.execute(
            """
            UPDATE alerts SET is_resolved = 1, resolved_at = ?
            WHERE is_resolved = 0 AND rule_name = 'missing_tracking'
              AND EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.receipt_id = alerts.order_id AND o.shop_name = alerts.shop_name
                    AND ((o.tracking_code IS NOT NULL AND TRIM(o.tracking_code) != '')
                     OR LOWER(o.status) IN ('canceled', 'refunded', 'fully refunded', 'cancelled'))
              )
            """,
            (now_str,),
        )
        resolved_count += c2.rowcount

        # 3. Resolve stuck_in_transit if order is now delivered or canceled/refunded
        c3 = conn.execute(
            """
            UPDATE alerts SET is_resolved = 1, resolved_at = ?
            WHERE is_resolved = 0 AND rule_name = 'stuck_in_transit'
              AND EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.receipt_id = alerts.order_id AND o.shop_name = alerts.shop_name
                    AND ((o.tracking_code IS NOT NULL AND TRIM(o.tracking_code) != '')
                     OR LOWER(o.status) IN ('delivered', 'completed delivered', 'canceled', 'refunded', 'fully refunded', 'cancelled'))
              )
            """,
            (now_str,),
        )
        resolved_count += c3.rowcount

    return resolved_count


def resolve_cleared_aggregate_alerts(active_keys: set, db_path: Optional[str] = None) -> int:
    """Resolve open refund_spike/buyer_velocity alerts no longer produced by the rules."""
    init_db(db_path)
    now_str = utc_now().isoformat()
    resolved = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, rule_name, order_id, shop_name FROM alerts "
            "WHERE is_resolved = 0 AND rule_name IN ('refund_spike', 'buyer_velocity')"
        ).fetchall()
        for r in rows:
            if (r["rule_name"], r["order_id"], r["shop_name"]) not in active_keys:
                conn.execute(
                    "UPDATE alerts SET is_resolved = 1, resolved_at = ? WHERE id = ?",
                    (now_str, r["id"]),
                )
                resolved += 1
    return resolved


def get_unsent_open_alerts(db_path: Optional[str] = None) -> List[Alert]:
    """Retrieve open alerts that were never delivered to Telegram."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM alerts WHERE is_resolved = 0 AND sent_to_telegram = 0 ORDER BY id"
        )
        return [_row_to_alert(row) for row in cursor.fetchall()]


def resolve_sync_stale_alerts(db_path: Optional[str] = None) -> int:
    """Resolve any open sync_stale alerts once a synchronization succeeds."""
    init_db(db_path)
    now_str = utc_now().isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE alerts SET is_resolved = 1, resolved_at = ?
            WHERE is_resolved = 0 AND rule_name = 'sync_stale'
            """,
            (now_str,),
        )
        return cursor.rowcount



def record_run(run_log: RunLog, db_path: Optional[str] = None) -> int:
    """Record execution details of a sync loop."""
    init_db(db_path)
    fin_str = run_log.finished_at.isoformat() if run_log.finished_at else None
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (started_at, finished_at, ok, orders_fetched, alerts_generated, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_log.started_at.isoformat(),
                fin_str,
                1 if run_log.ok else 0,
                run_log.orders_fetched,
                run_log.alerts_generated,
                run_log.error_message,
            ),
        )
        return cursor.lastrowid or 0


def get_last_run(db_path: Optional[str] = None) -> Optional[RunLog]:
    """Get the most recent run log record."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return None
        return RunLog(
            id=row["id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            ok=bool(row["ok"]),
            orders_fetched=row["orders_fetched"],
            alerts_generated=row["alerts_generated"],
            error_message=row["error_message"],
        )


def get_last_successful_run(db_path: Optional[str] = None) -> Optional[RunLog]:
    """Get the most recent successful run log record."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return None
        return RunLog(
            id=row["id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            ok=bool(row["ok"]),
            orders_fetched=row["orders_fetched"],
            alerts_generated=row["alerts_generated"],
            error_message=row["error_message"],
        )


def get_sync_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Calculate high-level dashboard and metrics statistics."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        total_orders = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]

        # Orders by shop
        shop_rows = conn.execute(
            "SELECT shop_name, COUNT(*) as c FROM orders GROUP BY shop_name"
        ).fetchall()
        orders_by_shop = {r["shop_name"]: r["c"] for r in shop_rows}

        # Revenue by currency
        rev_rows = conn.execute(
            "SELECT currency, SUM(total_amount) as total FROM orders GROUP BY currency"
        ).fetchall()
        revenue_by_currency = {
            r["currency"]: round(r["total"] or 0.0, 2) for r in rev_rows
        }

        # Open alerts breakdown
        open_alerts = conn.execute(
            "SELECT severity, COUNT(*) as c FROM alerts WHERE is_resolved = 0 GROUP BY severity"
        ).fetchall()
        alerts_by_severity = {r["severity"]: r["c"] for r in open_alerts}
        open_alerts_count = sum(alerts_by_severity.values())

        # Average fulfillment days (paid_at to shipped_at)
        avg_row = conn.execute(
            """
            SELECT AVG(
                (julianday(shipped_at) - julianday(paid_at))
            ) as avg_days
            FROM orders
            WHERE paid_at IS NOT NULL AND shipped_at IS NOT NULL AND is_shipped = 1
            """
        ).fetchone()
        avg_ship_days = (
            round(avg_row["avg_days"], 1) if avg_row and avg_row["avg_days"] is not None else None
        )

        last_run = get_last_run(db_path)

        return {
            "total_orders": total_orders,
            "orders_by_shop": orders_by_shop,
            "revenue_by_currency": revenue_by_currency,
            "open_alerts_count": open_alerts_count,
            "critical_alerts_count": alerts_by_severity.get("CRITICAL", 0),
            "warning_alerts_count": alerts_by_severity.get("WARNING", 0),
            "avg_fulfillment_days": avg_ship_days,
            "last_run": last_run.model_dump() if last_run else None,
        }
