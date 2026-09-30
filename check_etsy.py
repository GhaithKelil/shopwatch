"""Quick manual check that your Etsy credentials and tokens work.

Usage: python check_etsy.py <shop_name>
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from etsy import Etsy  # noqa: E402  (after load_dotenv so the keystring is picked up)

if len(sys.argv) != 2:
    sys.exit("Usage: python check_etsy.py <shop_name>")
if not os.environ.get("ETSY_KEYSTRING"):
    sys.exit("ETSY_KEYSTRING is not set. Copy .env.example to .env and fill it in.")

receipts = Etsy(sys.argv[1].lower()).receipts()
print("Orders found:", len(receipts))
for r in receipts[:3]:
    print(r["receipt_id"], r["status"], r["is_paid"], r["is_shipped"])
