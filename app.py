import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from database import (
    get_all_alerts,
    get_db_path,
    get_last_run,
    get_last_successful_run,
    get_open_alerts,
    get_order_count,
    get_recent_orders,
    get_sync_stats,
    init_db,
    resolve_alert,
    resolve_all_alerts,
)
from models import Alert, Order, RunLog
from scheduler import run_sync_cycle

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database on startup
    init_db()
    yield


app = FastAPI(
    title="ShopWatch API",
    description="Operational Etsy order monitoring and problem detection service for one or more shops.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Monitoring"])
def health_check(response: Response) -> Dict[str, Any]:
    """Health check endpoint for Docker container probes and load balancers."""
    try:
        stats = get_sync_stats()
        last_success = get_last_successful_run()
        sync_stale = False
        if last_success is None and get_last_run() is not None:
            sync_stale = True
        if last_success and last_success.finished_at:
            from datetime import timedelta
            from models import utc_now
            stale_threshold = float(os.environ.get("SYNC_STALE_MAX_HOURS", 2.0))
            if (utc_now() - last_success.finished_at) > timedelta(hours=stale_threshold):
                sync_stale = True

        if sync_stale:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return {
            "status": "degraded" if sync_stale else "ok",
            "database_connected": True,
            "sync_healthy": not sync_stale,
            "total_orders": stats["total_orders"],
            "open_alerts": stats["open_alerts_count"],
            "last_successful_sync": last_success.finished_at.isoformat() if last_success and last_success.finished_at else None,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service unhealthy: {exc}",
        )


@app.get("/metrics", tags=["Monitoring"])
@app.get("/api/stats", tags=["Monitoring"])
def get_stats() -> Dict[str, Any]:
    """Operational metrics overview covering order volume, alerts, and fulfillment times."""
    return get_sync_stats()


@app.get("/api/orders", tags=["Orders"])
def list_orders(
    shop: Optional[str] = Query(None, description="Filter by shop name (e.g. myshop)"),
    status: Optional[str] = Query(None, description="Filter by order status"),
    search: Optional[str] = Query(None, description="Search receipt ID, buyer name, or buyer ID"),
    limit: int = Query(50, ge=1, le=5000, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
) -> Dict[str, Any]:
    """List orders with pagination, shop filtering, and search."""
    orders = get_recent_orders(limit=limit, offset=offset, shop_name=shop, status=status, search=search)
    total = get_order_count(shop_name=shop, status=status, search=search)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "orders": [o.model_dump() for o in orders],
    }


@app.get("/api/alerts", tags=["Alerts"])
def list_alerts(
    all: bool = Query(False, description="Set true to return both resolved and open alerts"),
    limit: int = Query(100, ge=1, le=1000),
) -> List[Dict[str, Any]]:
    """List active open alerts or historical alerts."""
    if all:
        alerts = get_all_alerts(limit=limit)
    else:
        alerts = get_open_alerts()
    return [a.model_dump() for a in alerts]


@app.post("/api/alerts/{alert_id}/resolve", tags=["Alerts"])
def resolve_alert_endpoint(alert_id: int) -> Dict[str, Any]:
    """Mark an open alert as resolved."""
    success = resolve_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Alert #{alert_id} not found or already resolved.")
    return {"status": "ok", "alert_id": alert_id, "resolved": True}


@app.post("/api/alerts/resolve-all", tags=["Alerts"])
def resolve_all_alerts_endpoint(
    severity: Optional[str] = Query(None, description="Optional severity filter (CRITICAL or WARNING)"),
) -> Dict[str, Any]:
    """Mark open alerts as resolved, optionally filtered by severity."""
    count = resolve_all_alerts(severity=severity)
    return {"status": "ok", "resolved_count": count}


@app.post("/api/sync", tags=["Pipeline"])
def trigger_sync() -> Dict[str, Any]:
    """Manually trigger an on-demand sync cycle across all configured data sources."""
    try:
        run_log = run_sync_cycle()
        return {
            "status": "ok",
            "run": run_log.model_dump(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync execution failed: {exc}")


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"], include_in_schema=False)
def dashboard() -> str:
    """Serve the operations dashboard."""
    return DASHBOARD_PATH.read_text(encoding="utf-8")
