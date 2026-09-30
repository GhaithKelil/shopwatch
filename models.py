from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


class Order(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    receipt_id: str
    shop_name: str
    source: str = "csv"  # "csv" or "api"
    created_at: datetime
    paid_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    status: str = "Paid"
    total_amount: float = 0.0
    currency: str = "EUR"
    country: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_id: Optional[str] = None
    shipping_address: Optional[str] = None
    tracking_code: Optional[str] = None
    is_paid: bool = True
    is_shipped: bool = False


class Alert(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    order_id: Optional[str] = None
    shop_name: str
    rule_name: str
    severity: str = "WARNING"  # "CRITICAL", "WARNING", "INFO"
    message: str
    created_at: datetime = Field(default_factory=utc_now)
    is_resolved: bool = False
    sent_to_telegram: bool = False
    resolved_at: Optional[datetime] = None



class RunLog(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    ok: bool = True
    orders_fetched: int = 0
    alerts_generated: int = 0
    error_message: Optional[str] = None
