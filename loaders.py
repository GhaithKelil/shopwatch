import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from models import Order

logger = logging.getLogger(__name__)


def parse_date(value: Any) -> Optional[datetime]:
    """Parse date from multiple possible formats into timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    val_str = str(value).strip()
    if not val_str or val_str.lower() in ("none", "null", ""):
        return None

    # Handle string of digits or float as unix timestamp
    try:
        ts = float(val_str)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        if 1e8 <= ts <= 4e9:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError):
        pass


    formats = [
        "%m/%d/%y",              # 09/29/26
        "%m/%d/%Y",              # 09/29/2026
        "%Y-%m-%d",              # 2026-09-29
        "%Y-%m-%dT%H:%M:%S%z",   # ISO with timezone
        "%Y-%m-%dT%H:%M:%SZ",    # ISO Zulu
        "%Y-%m-%d %H:%M:%S",     # Standard SQL datetime
        "%d/%m/%Y",              # European format
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(val_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Attempt fromisoformat as fallback
    try:
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.warning(f"Could not parse date string: '{val_str}'")
        return None


class CsvOrderLoader:
    """Ingests and normalizes Etsy Order CSV exports into standard Order models."""

    def __init__(self, shop_name: str = "myshop"):
        self.shop_name = shop_name

    def load_from_file(self, file_path: Union[str, Path]) -> List[Order]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Order CSV file not found: {path}")

        # Try utf-8-sig first to strip potential BOM, fallback to latin-1
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                content = f.read()

        return self.load_from_string(content)

    def load_from_string(self, csv_content: str) -> List[Order]:
        reader = csv.DictReader(io.StringIO(csv_content))
        orders: List[Order] = []

        for row in reader:
            order_id = row.get("Order ID", "").strip()
            if not order_id:
                continue

            sale_date_raw = row.get("Sale Date", "").strip()
            created_at = parse_date(sale_date_raw) or datetime.now(timezone.utc)
            # In Etsy sales CSV, sale date also signifies payment confirmation
            paid_at = created_at

            date_shipped_raw = row.get("Date Shipped", "").strip()
            shipped_at = parse_date(date_shipped_raw) if date_shipped_raw else None
            is_shipped = shipped_at is not None

            # Status derivation
            raw_status = row.get("Status", "").strip()
            if raw_status:
                status = raw_status
            elif is_shipped:
                status = "Completed"
            else:
                status = "Paid"

            # Check for refund/cancellation cues
            adjusted_net = row.get("Adjusted Net Order Amount", "0.00").strip()
            if "cancel" in raw_status.lower():
                status = "Canceled"
            elif "refund" in raw_status.lower():
                status = "Refunded"
            elif adjusted_net and adjusted_net != "0.00":
                try:
                    adj_val = float(adjusted_net.replace("$", "").replace("â‚¬", "").replace(",", ""))
                    if adj_val < 0:
                        status = "Refunded"
                except ValueError:
                    pass


            # Parse amounts safely
            total_raw = row.get("Order Total", "0").replace("$", "").replace("â‚¬", "").replace(",", "").strip()
            try:
                total_amount = float(total_raw) if total_raw else 0.0
            except ValueError:
                total_amount = 0.0

            currency = row.get("Currency", "EUR").strip() or "EUR"
            buyer_id = row.get("Buyer User ID", "").strip() or None
            buyer_name = row.get("Full Name", "").strip() or row.get("Buyer", "").strip() or None
            country = row.get("Ship Country", "").strip() or None

            # Aggregate shipping address
            addr_parts = [
                row.get("Street 1", "").strip(),
                row.get("Street 2", "").strip(),
                row.get("Ship City", "").strip(),
                row.get("Ship State", "").strip(),
                row.get("Ship Zipcode", "").strip(),
                country or "",
            ]
            shipping_address = ", ".join(p for p in addr_parts if p) or None

            # Tracking code
            tracking_code = (
                row.get("Tracking Code", "")
                or row.get("Tracking Number", "")
                or row.get("Tracking", "")
            ).strip() or None

            orders.append(
                Order(
                    receipt_id=order_id,
                    shop_name=self.shop_name,
                    source="csv",
                    created_at=created_at,
                    paid_at=paid_at,
                    shipped_at=shipped_at,
                    status=status,
                    total_amount=total_amount,
                    currency=currency,
                    country=country,
                    buyer_name=buyer_name,
                    buyer_id=buyer_id,
                    shipping_address=shipping_address,
                    tracking_code=tracking_code,
                    is_paid=True,
                    is_shipped=is_shipped,
                )
            )

        return orders


class ApiOrderLoader:
    """Normalizes raw Etsy v3 API receipt payloads into standard Order models."""

    def __init__(self, shop_name: str = "myshop"):
        self.shop_name = shop_name

    def normalize_receipt(self, receipt: Dict[str, Any]) -> Order:
        receipt_id = str(receipt.get("receipt_id", "")).strip()

        # Timestamp parsing
        created_raw = (
            receipt.get("created_timestamp")
            or receipt.get("creation_timestamp")
            or receipt.get("create_date")
        )
        created_at = parse_date(created_raw) or datetime.now(timezone.utc)

        paid_raw = receipt.get("paid_timestamp") or receipt.get("paid_date")
        is_paid = bool(receipt.get("is_paid", True))
        paid_at = parse_date(paid_raw) if paid_raw else (created_at if is_paid else None)

        shipped_raw = receipt.get("shipped_timestamp") or receipt.get("shipped_date")
        is_shipped = bool(receipt.get("is_shipped", False))
        if not shipped_raw:
            # Etsy v3 receipts carry the real ship date on the shipment notification
            for s in receipt.get("shipments") or []:
                if isinstance(s, dict) and s.get("shipment_notification_timestamp"):
                    shipped_raw = s["shipment_notification_timestamp"]
                    break
        shipped_at = parse_date(shipped_raw) if shipped_raw else (paid_at or created_at if is_shipped else None)


        # Amount and currency from grandtotal or raw fields
        grandtotal = receipt.get("grandtotal")
        total_amount = 0.0
        currency = "USD"
        if isinstance(grandtotal, dict):
            raw_amt = grandtotal.get("amount", 0)
            divisor = grandtotal.get("divisor", 100) or 100
            total_amount = float(raw_amt) / float(divisor)
            currency = grandtotal.get("currency_code", "USD")
        elif grandtotal is not None:
            try:
                total_amount = float(grandtotal)
            except (ValueError, TypeError):
                total_amount = 0.0
            currency = receipt.get("currency_code", "USD")
        else:
            total_price = receipt.get("total_price", 0.0)
            try:
                total_amount = float(total_price)
            except (ValueError, TypeError):
                total_amount = 0.0
            currency = receipt.get("currency_code", "USD")

        # Buyer info
        buyer_name = receipt.get("name") or receipt.get("buyer_email") or None
        buyer_id = str(receipt.get("buyer_user_id")) if receipt.get("buyer_user_id") else None

        # Tracking code from shipments
        tracking_code = None
        shipments = receipt.get("shipments") or []
        if isinstance(shipments, list) and len(shipments) > 0:
            first_shipment = shipments[0]
            if isinstance(first_shipment, dict):
                tracking_code = first_shipment.get("tracking_code") or None

        # Destination & Address
        country = receipt.get("country_iso") or None
        addr_parts = [
            receipt.get("first_line", ""),
            receipt.get("second_line", ""),
            receipt.get("city", ""),
            receipt.get("state", ""),
            receipt.get("zip", ""),
            country or "",
        ]
        shipping_address = ", ".join(p for p in addr_parts if p) or None

        status = receipt.get("status", "Completed" if is_shipped else ("Paid" if is_paid else "Open"))

        return Order(
            receipt_id=receipt_id,
            shop_name=self.shop_name,
            source="api",
            created_at=created_at,
            paid_at=paid_at,
            shipped_at=shipped_at,
            status=status,
            total_amount=round(total_amount, 2),
            currency=currency,
            country=country,
            buyer_name=buyer_name,
            buyer_id=buyer_id,
            shipping_address=shipping_address,
            tracking_code=tracking_code,
            is_paid=is_paid,
            is_shipped=is_shipped,
        )

    def load_receipts(self, receipts: List[Dict[str, Any]]) -> List[Order]:
        return [self.normalize_receipt(r) for r in receipts if r.get("receipt_id")]
