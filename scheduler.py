import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from database import (
    auto_resolve_cleared_alerts,
    get_all_orders_for_rules,
    get_db_path,
    get_last_run,
    get_last_successful_run,
    record_run,
    resolve_cleared_aggregate_alerts,
    resolve_sync_stale_alerts,
    upsert_orders_bulk,
)

from loaders import ApiOrderLoader, CsvOrderLoader
from models import Order, RunLog, utc_now
from notifier import dispatch_alerts
from rules import evaluate_rules

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("shopwatch.scheduler")

_SHUTDOWN_REQUESTED = False


def _handle_shutdown(signum, frame):
    global _SHUTDOWN_REQUESTED
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    _SHUTDOWN_REQUESTED = True


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


def get_shop_names() -> list[str]:
    """Shops to monitor, from the comma-separated SHOP_NAMES setting."""
    names = [n.strip() for n in os.environ.get("SHOP_NAMES", "").split(",") if n.strip()]
    return names or ["myshop"]


def get_tokens_path(shop: str) -> str:
    return os.path.join(os.environ.get("TOKENS_DIR", "."), f"tokens_{shop.lower()}.json")


def _ingest_shop(shop: str, csv_file: Optional[str], single_shop: bool, errors: list[str]) -> list[Order]:
    """Fetch one shop's orders from the Etsy API, falling back to a CSV export."""
    orders: list[Order] = []

    # 1. Etsy API
    token_path = get_tokens_path(shop)
    etsy_key = os.environ.get("ETSY_KEYSTRING", "").strip()

    if os.path.exists(token_path) and etsy_key:
        try:
            logger.info(f"Ingesting live API receipts for shop '{shop}'...")
            from etsy import Etsy

            receipts = Etsy(shop.lower()).receipts()
            orders.extend(ApiOrderLoader(shop_name=shop).load_receipts(receipts))
            logger.info(f"Loaded {len(orders)} orders from Etsy API for '{shop}'.")
        except Exception as exc:
            err = f"Failed to ingest orders via Etsy API for '{shop}': {exc}"
            logger.error(err)
            errors.append(err)
    elif not etsy_key:
        logger.info(f"ETSY_KEYSTRING not configured; using CSV fallback for '{shop}'.")
    else:
        logger.info(f"Token file '{token_path}' not found; using CSV fallback for '{shop}'.")

    # 2. CSV export: used when the API gave nothing, or when a file is passed explicitly
    candidates = [csv_file, f"orders_{shop.lower()}.csv"]
    if single_shop:
        candidates[1:1] = [os.environ.get("CSV_PATH"), "orders.csv"]
    active_csv = next((c for c in candidates if c and os.path.exists(c)), None)

    if (not orders or csv_file is not None) and active_csv:
        try:
            logger.info(f"Ingesting CSV orders from '{active_csv}' for shop '{shop}'...")
            csv_orders = CsvOrderLoader(shop_name=shop).load_from_file(active_csv)
            orders.extend(csv_orders)
            logger.info(f"Loaded {len(csv_orders)} orders from CSV.")
        except Exception as exc:
            err = f"Failed to ingest CSV '{active_csv}': {exc}"
            logger.error(err)
            errors.append(err)
    return orders


def run_sync_cycle(
    db_path: Optional[str] = None,
    csv_file: Optional[str] = None,
    shop_name: Optional[str] = None,
) -> RunLog:
    """
    Execute one ingestion, evaluation and alerting cycle for every configured shop.
    Orders come from the Etsy API when credentials exist, with CSV export as a fallback.
    """
    shops = [shop_name] if shop_name else get_shop_names()
    started_at = utc_now()
    logger.info(f"Starting ShopWatch synchronization cycle for {', '.join(shops)}...")
    fetched_orders: list[Order] = []
    errors: list[str] = []

    # 1-2. Ingest each shop
    for shop in shops:
        fetched_orders.extend(_ingest_shop(shop, csv_file, len(shops) == 1, errors))

    # 3. Upsert newly fetched orders into SQLite storage
    if fetched_orders:
        upsert_orders_bulk(fetched_orders, db_path=db_path)
        logger.info(f"Persisted/updated {len(fetched_orders)} orders in database.")

    # 4. Fetch state and run Problem Detection Rules
    all_orders = get_all_orders_for_rules(db_path=db_path)
    last_success = get_last_successful_run(db_path=db_path)
    last_run = get_last_run(db_path=db_path)

    # If the system has never recorded any run at all, this is initial setup,
    # so we treat started_at as healthy baseline rather than alerting 'never run'.
    # If runs exist but none succeeded, last_success_time remains None (triggers alert).
    if last_run is None:
        last_success_time = started_at
    else:
        last_success_time = last_success.finished_at if last_success else None

    # Load custom thresholds from environment if defined
    rule_config = {
        "late_unshipped_days": int(os.environ.get("LATE_UNSHIPPED_DAYS", 3)),
        "stuck_transit_days": int(os.environ.get("STUCK_TRANSIT_DAYS", 14)),
        "stuck_transit_max_days": int(os.environ.get("STUCK_TRANSIT_MAX_DAYS", 60)),
        "refund_spike_pct": float(os.environ.get("REFUND_SPIKE_THRESHOLD_PCT", 10.0)),
        "velocity_max_orders": int(os.environ.get("BUYER_VELOCITY_MAX_ORDERS", 3)),
        "velocity_window_hours": int(os.environ.get("BUYER_VELOCITY_WINDOW_HOURS", 24)),
        "sync_stale_hours": float(os.environ.get("SYNC_STALE_MAX_HOURS", 2.0)),
    }

    generated_alerts = evaluate_rules(
        all_orders,
        last_successful_run=last_success_time,
        now=started_at,
        config=rule_config,
    )
    logger.info(f"Evaluated 6 rules against {len(all_orders)} orders: {len(generated_alerts)} raw alerts found.")

    # 5. Deduplicate and dispatch alerts
    resolve_cleared_aggregate_alerts(
        {(a.rule_name, a.order_id, a.shop_name) for a in generated_alerts},
        db_path=db_path,
    )
    new_alerts_count = dispatch_alerts(generated_alerts, db_path=db_path)
    logger.info(f"Dispatched {new_alerts_count} new alerts after deduplication.")

    finished_at = utc_now()
    is_ok = len(errors) == 0
    error_summary = "; ".join(errors) if errors else None

    # 6. Auto-resolve alerts whose conditions have cleared
    auto_resolved_count = auto_resolve_cleared_alerts(db_path=db_path)
    if is_ok:
        resolve_sync_stale_alerts(db_path=db_path)
    if auto_resolved_count > 0:
        logger.info(f"Auto-resolved {auto_resolved_count} previously open alerts that have been cleared.")


    run_log = RunLog(
        started_at=started_at,
        finished_at=finished_at,
        ok=is_ok,
        orders_fetched=len(fetched_orders),
        alerts_generated=new_alerts_count,
        error_message=error_summary,
    )
    record_run(run_log, db_path=db_path)
    logger.info(
        f"Cycle completed in {(finished_at - started_at).total_seconds():.2f}s "
        f"(status={'OK' if is_ok else 'FAILED'})."
    )
    return run_log


def main():
    parser = argparse.ArgumentParser(description="ShopWatch Continuous Order Monitoring Daemon")
    parser.add_argument("--once", action="store_true", help="Execute single sync cycle and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SYNC_INTERVAL_SECONDS", 900)),
        help="Polling interval in seconds between cycles (default: 900 / 15m)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Custom SQLite database path",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Path to CSV order file (default: none; CSV is only a fallback when the API returns no orders)",
    )
    args = parser.parse_args()

    db_path = get_db_path(args.db)
    logger.info(f"Starting ShopWatch daemon (interval={args.interval}s, db='{db_path}')")

    if args.once:
        run_sync_cycle(db_path=db_path, csv_file=args.csv)
        logger.info("Single sync run finished.")
        return

    while not _SHUTDOWN_REQUESTED:
        try:
            run_sync_cycle(db_path=db_path, csv_file=args.csv)
        except Exception as exc:
            logger.exception(f"Unexpected error in sync daemon loop: {exc}")

        # Sleep in small slices to remain responsive to SIGINT/SIGTERM
        for _ in range(args.interval):
            if _SHUTDOWN_REQUESTED:
                break
            time.sleep(1)

    logger.info("ShopWatch scheduler stopped cleanly.")


if __name__ == "__main__":
    main()
