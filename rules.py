from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from models import Alert, Order, utc_now

INACTIVE_STATUSES = ("canceled", "cancelled", "refunded", "fully refunded")
DEFAULT_LATE_UNSHIPPED_DAYS = 3
DEFAULT_STUCK_TRANSIT_DAYS = 14
DEFAULT_REFUND_SPIKE_PCT = 10.0
DEFAULT_REFUND_SPIKE_WINDOW_DAYS = 30
DEFAULT_VELOCITY_MAX_ORDERS = 3
DEFAULT_VELOCITY_WINDOW_HOURS = 24
DEFAULT_SYNC_STALE_HOURS = 2.0


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def rule_late_unshipped(
    order: Order,
    now: Optional[datetime] = None,
    threshold_days: int = DEFAULT_LATE_UNSHIPPED_DAYS,
) -> Optional[Alert]:
    """
    Flag orders that have been paid but remain unshipped beyond threshold_days.
    Severity: CRITICAL
    """
    if order.status.lower() in INACTIVE_STATUSES:
        return None

    if order.is_shipped or order.shipped_at is not None:
        return None

    if not order.is_paid and not order.paid_at:
        return None

    ref_time = _ensure_utc(order.paid_at or order.created_at)
    if not ref_time:
        return None

    current_time = _ensure_utc(now) or utc_now()
    age = current_time - ref_time
    threshold = timedelta(days=threshold_days)

    if age >= threshold:
        days = round(age.total_seconds() / 86400, 1)
        paid_date = ref_time.strftime("%Y-%m-%d")
        return Alert(
            order_id=order.receipt_id,
            shop_name=order.shop_name,
            rule_name="late_unshipped",
            severity="CRITICAL",
            message=(
                f"Order #{order.receipt_id} ({order.shop_name}) paid on {paid_date} "
                f"has remained unshipped for {days} days (threshold: {threshold_days} days)."
            ),
            created_at=current_time,
        )
    return None


def rule_missing_tracking(
    order: Order,
    now: Optional[datetime] = None,
) -> Optional[Alert]:
    """
    Flag orders marked shipped but lacking a tracking number.
    Severity: WARNING
    """
    if order.status.lower() in INACTIVE_STATUSES:
        return None

    is_shipped = order.is_shipped or (order.shipped_at is not None)
    if not is_shipped:
        return None

    tracking = (order.tracking_code or "").strip()
    if not tracking:
        shipped_str = (
            order.shipped_at.strftime("%Y-%m-%d") if order.shipped_at else "Unknown date"
        )
        current_time = _ensure_utc(now) or utc_now()
        return Alert(
            order_id=order.receipt_id,
            shop_name=order.shop_name,
            rule_name="missing_tracking",
            severity="WARNING",
            message=(
                f"Order #{order.receipt_id} ({order.shop_name}) was marked shipped on "
                f"{shipped_str} without tracking code."
            ),
            created_at=current_time,
        )
    return None


def rule_stuck_in_transit(
    order: Order,
    now: Optional[datetime] = None,
    threshold_days: int = DEFAULT_STUCK_TRANSIT_DAYS,
    max_days: Optional[int] = None,
) -> Optional[Alert]:
    """
    Flag shipped orders in transit for more than threshold_days without delivery confirmation.
    Severity: WARNING
    """
    if order.status.lower() in INACTIVE_STATUSES + ("delivered", "completed delivered", "closed"):
        return None

    if not order.shipped_at:
        return None

    # Etsy never reports delivery, so only untracked shipments can't be followed up
    if (order.tracking_code or "").strip():
        return None

    shipped_time = _ensure_utc(order.shipped_at)
    current_time = _ensure_utc(now) or utc_now()
    age = current_time - shipped_time
    threshold = timedelta(days=threshold_days)

    if age >= threshold:
        if max_days and age > timedelta(days=max_days):
            return None
        days = round(age.total_seconds() / 86400, 1)
        shipped_date = shipped_time.strftime("%Y-%m-%d")
        return Alert(
            order_id=order.receipt_id,
            shop_name=order.shop_name,
            rule_name="stuck_in_transit",
            severity="WARNING",
            message=(
                f"Order #{order.receipt_id} ({order.shop_name}) shipped {days} days ago on "
                f"{shipped_date} has not reached delivered status (threshold: {threshold_days} days)."
            ),
            created_at=current_time,
        )
    return None



def rule_refund_spike(
    orders: List[Order],
    now: Optional[datetime] = None,
    window_days: int = DEFAULT_REFUND_SPIKE_WINDOW_DAYS,
    threshold_pct: float = DEFAULT_REFUND_SPIKE_PCT,
    min_orders: int = 5,
) -> List[Alert]:
    """
    Flag shops experiencing refund/cancellation rates above threshold_pct in the trailing window.
    Severity: CRITICAL
    """
    current_time = _ensure_utc(now) or utc_now()
    cutoff = current_time - timedelta(days=window_days)

    # Group recent orders by shop
    by_shop: Dict[str, List[Order]] = defaultdict(list)
    for o in orders:
        created = _ensure_utc(o.created_at)
        if created and created >= cutoff:
            by_shop[o.shop_name].append(o)

    alerts: List[Alert] = []
    for shop_name, shop_orders in by_shop.items():
        total_count = len(shop_orders)
        if total_count < min_orders:
            continue

        refunded_count = sum(
            1
            for o in shop_orders
            if o.status.lower() in INACTIVE_STATUSES
        )
        pct = (refunded_count / total_count) * 100.0

        if pct >= threshold_pct and refunded_count > 0:
            alerts.append(
                Alert(
                    order_id=None,
                    shop_name=shop_name,
                    rule_name="refund_spike",
                    severity="CRITICAL",
                    message=(
                        f"Refund/Cancellation spike detected in shop '{shop_name}': "
                        f"{pct:.1f}% ({refunded_count}/{total_count} orders) over the past "
                        f"{window_days} days exceeds {threshold_pct}% threshold."
                    ),
                    created_at=current_time,
                )
            )
    return alerts


def rule_fraud_velocity(
    orders: List[Order],
    now: Optional[datetime] = None,
    window_hours: int = DEFAULT_VELOCITY_WINDOW_HOURS,
    max_orders: int = DEFAULT_VELOCITY_MAX_ORDERS,
) -> List[Alert]:
    """
    Flag buyers placing >= max_orders within a sliding window_hours period.
    Severity: WARNING
    """
    current_time = _ensure_utc(now) or utc_now()
    cutoff = current_time - timedelta(hours=window_hours)

    # Group by (shop_name, buyer_key) where buyer_key is buyer_id or buyer_name
    groups: Dict[tuple, List[Order]] = defaultdict(list)
    for o in orders:
        created = _ensure_utc(o.created_at)
        if not created or created < cutoff:
            continue

        # Ignore canceled orders for velocity check
        if o.status.lower() in INACTIVE_STATUSES:
            continue

        buyer_key = (
            (o.buyer_id or "").strip().lower()
            or (o.buyer_name or "").strip().lower()
            or (o.shipping_address or "").strip().lower()
        )
        if buyer_key:
            groups[(o.shop_name, buyer_key)].append(o)

    alerts: List[Alert] = []
    for (shop_name, buyer_key), buyer_orders in groups.items():
        if len(buyer_orders) >= max_orders:
            order_ids = sorted((o.receipt_id for o in buyer_orders), key=lambda r: (len(r), r))
            sample_name = buyer_orders[0].buyer_name or buyer_key
            alerts.append(
                Alert(
                    order_id=order_ids[-1],  # Associate with most recent order ID
                    shop_name=shop_name,
                    rule_name="buyer_velocity",
                    severity="WARNING",
                    message=(
                        f"Rapid buyer velocity detected for '{sample_name}' in shop '{shop_name}': "
                        f"{len(buyer_orders)} orders placed within {window_hours} hours. "
                        f"Order IDs: {', '.join(order_ids)}."
                    ),
                    created_at=current_time,
                )
            )
    return alerts


def rule_sync_stale(
    last_successful_run: Optional[datetime],
    now: Optional[datetime] = None,
    max_stale_hours: float = DEFAULT_SYNC_STALE_HOURS,
) -> Optional[Alert]:
    """
    Flag when the ingestion pipeline has not completed a successful cycle within max_stale_hours.
    Severity: CRITICAL
    """
    current_time = _ensure_utc(now) or utc_now()

    if last_successful_run is None:
        return Alert(
            order_id=None,
            shop_name="SYSTEM",
            rule_name="sync_stale",
            severity="CRITICAL",
            message="Ingestion pipeline has never completed a successful synchronization run.",
            created_at=current_time,
        )

    last_time = _ensure_utc(last_successful_run)
    elapsed = current_time - last_time
    stale_delta = timedelta(hours=max_stale_hours)

    if elapsed >= stale_delta:
        hours = round(elapsed.total_seconds() / 3600, 1)
        last_str = last_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        return Alert(
            order_id=None,
            shop_name="SYSTEM",
            rule_name="sync_stale",
            severity="CRITICAL",
            message=(
                f"Sync pipeline is stale! Last successful run was {last_str} "
                f"({hours} hours ago). Exceeds operational tolerance of {max_stale_hours} hours."
            ),
            created_at=current_time,
        )
    return None


def evaluate_rules(
    orders: List[Order],
    last_successful_run: Optional[datetime] = None,
    now: Optional[datetime] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Alert]:
    """Run all 6 business rules across orders and system state, returning generated alerts."""
    cfg = config or {}
    current_time = _ensure_utc(now) or utc_now()
    alerts: List[Alert] = []

    late_days = cfg.get("late_unshipped_days", DEFAULT_LATE_UNSHIPPED_DAYS)
    transit_days = cfg.get("stuck_transit_days", DEFAULT_STUCK_TRANSIT_DAYS)
    transit_max_days = cfg.get("stuck_transit_max_days", None)
    refund_pct = cfg.get("refund_spike_pct", DEFAULT_REFUND_SPIKE_PCT)
    refund_window = cfg.get("refund_spike_window_days", DEFAULT_REFUND_SPIKE_WINDOW_DAYS)
    velocity_max = cfg.get("velocity_max_orders", DEFAULT_VELOCITY_MAX_ORDERS)
    velocity_window = cfg.get("velocity_window_hours", DEFAULT_VELOCITY_WINDOW_HOURS)
    stale_hours = cfg.get("sync_stale_hours", DEFAULT_SYNC_STALE_HOURS)

    # Per-order rules
    for order in orders:
        alert = rule_late_unshipped(order, now=current_time, threshold_days=late_days)
        if alert:
            alerts.append(alert)

        alert = rule_missing_tracking(order, now=current_time)
        if alert:
            alerts.append(alert)

        alert = rule_stuck_in_transit(
            order,
            now=current_time,
            threshold_days=transit_days,
            max_days=transit_max_days,
        )
        if alert:
            alerts.append(alert)


    # Aggregate & shop-level rules
    alerts.extend(
        rule_refund_spike(
            orders,
            now=current_time,
            window_days=refund_window,
            threshold_pct=refund_pct,
        )
    )
    alerts.extend(
        rule_fraud_velocity(
            orders,
            now=current_time,
            window_hours=velocity_window,
            max_orders=velocity_max,
        )
    )

    # System heartbeat rule
    stale_alert = rule_sync_stale(
        last_successful_run,
        now=current_time,
        max_stale_hours=stale_hours,
    )
    if stale_alert:
        alerts.append(stale_alert)

    return alerts
