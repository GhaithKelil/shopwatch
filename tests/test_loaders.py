from datetime import datetime, timezone
import pytest
from loaders import parse_date, CsvOrderLoader, ApiOrderLoader


def test_parse_date_various_formats():
    # 2-digit year
    d1 = parse_date("09/29/26")
    assert d1 == datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)

    # 4-digit year
    d2 = parse_date("09/29/2026")
    assert d2 == datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)

    # ISO format
    d3 = parse_date("2026-09-29T14:30:00Z")
    assert d3 == datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)

    # Unix timestamp (seconds int and string)
    d4 = parse_date(1790683800)
    assert d4.year == 2026
    d5 = parse_date("1790683800")
    assert d5 is not None
    assert d5.year == 2026

    # None and empty
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("invalid_date_xyz") is None



def test_csv_order_loader_string():
    csv_text = """Sale Date,Order ID,Buyer User ID,Full Name,First Name,Last Name,Number of Items,Payment Method,Date Shipped,Street 1,Street 2,Ship City,Ship State,Ship Zipcode,Ship Country,Currency,Order Value,Coupon Code,Coupon Details,Discount Amount,Shipping Discount,Shipping,Sales Tax,Order Total,Status,Card Processing Fees,Order Net,Adjusted Order Total,Adjusted Card Processing Fees,Adjusted Net Order Amount,Buyer,Order Type,Payment Type,InPerson Discount,InPerson Location,SKU
09/25/26,11223344,buyer01,"Jane Doe",Jane,Doe,1,"Credit Card",09/27/26,"100 Main St",,"Boston",MA,02108,"United States",USD,50.00,,,0.00,0.00,5.00,0,55.00,,2.00,53.00,0.00,0.00,0.00,"Jane Doe",online,online_cc,,,SKU-101
"""
    loader = CsvOrderLoader("ExampleShop")
    orders = loader.load_from_string(csv_text)
    assert len(orders) == 1
    o = orders[0]
    assert o.receipt_id == "11223344"
    assert o.shop_name == "ExampleShop"
    assert o.buyer_name == "Jane Doe"
    assert o.buyer_id == "buyer01"
    assert o.total_amount == 55.00
    assert o.currency == "USD"
    assert o.country == "United States"
    assert o.is_paid is True
    assert o.is_shipped is True
    assert o.status == "Completed"


def test_csv_order_loader_real_file():
    import os
    loader = CsvOrderLoader("ExampleShop")
    csv_path = os.path.join(os.path.dirname(__file__), "fixtures", "sample_orders.csv")
    orders = loader.load_from_file(csv_path)
    assert len(orders) >= 2
    assert all(o.shop_name == "ExampleShop" for o in orders)
    assert all(o.source == "csv" for o in orders)


def test_api_order_loader_receipt():
    payload = {
        "receipt_id": 987654321,
        "created_timestamp": 1790683800,
        "paid_timestamp": 1790683800,
        "shipped_timestamp": 1790770200,
        "status": "Completed",
        "is_paid": True,
        "is_shipped": True,
        "grandtotal": {
            "amount": 4250,
            "divisor": 100,
            "currency_code": "EUR"
        },
        "name": "Erika Mustermann",
        "buyer_user_id": 554433,
        "first_line": "Musterweg 1",
        "city": "Munich",
        "state": "BY",
        "zip": "80331",
        "country_iso": "DE",
        "shipments": [
            {
                "tracking_code": "DHL12345678DE",
                "carrier_name": "dhl"
            }
        ]
    }

    loader = ApiOrderLoader("ExampleShop")
    order = loader.normalize_receipt(payload)

    assert order.receipt_id == "987654321"
    assert order.shop_name == "ExampleShop"
    assert order.source == "api"
    assert order.total_amount == 42.50
    assert order.currency == "EUR"
    assert order.buyer_name == "Erika Mustermann"
    assert order.buyer_id == "554433"
    assert order.tracking_code == "DHL12345678DE"
    assert order.country == "DE"
    assert order.is_paid is True
    assert order.is_shipped is True
