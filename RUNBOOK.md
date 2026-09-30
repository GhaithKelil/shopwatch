# Runbook

What breaks, how to spot it, how to fix it.

## Quick checks

```bash
curl http://localhost:8000/health          # 200 = fine, 503 = sync is stale or failing
docker compose logs -f worker              # Docker: watch the sync loop
python scheduler.py --once                 # run one sync by hand and read the output
```

The dashboard's **Last sync** tile shows when the last run happened and whether it succeeded.

## Problems

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| Logs show `401 Client Error`, or a "Sync stale" alert | Etsy token expired or was revoked | Re-authorize: `python auth.py <shop>`, then copy the new `tokens_<shop>.json` into place (Docker: `./tokens/`) |
| `429 Too Many Requests` in the last run's error | Etsy rate limit | Raise `SYNC_INTERVAL_SECONDS` (for example 1800) |
| Sync runs but loads 0 orders | No token file or `ETSY_KEYSTRING`, and no CSV found | Check `SHOP_NAMES` matches the token file name, or that `orders_<shop>.csv` exists |
| CSV loads 0 orders or fails | Etsy changed the export columns | Compare the header row with `tests/fixtures/sample_orders.csv`, add the column names in `loaders.py`, run `pytest tests/test_loaders.py` |
| No Telegram messages | Missing or wrong token or chat ID, or Telegram unreachable | Unsent alerts are kept and retried each sync. Test: `python -c "from notifier import send_telegram_message; print(send_telegram_message('test'))"` |
| `database is locked` | Something else holds the database open | Stop extra processes using the same file. Normal use (one worker, one web server) does not hit this |
| `/health` returns 503 | Last successful sync is older than `SYNC_STALE_MAX_HOURS`, or every run failed | Read the worker logs; fix the cause above |
| A shop's alerts look wrong after adding a second shop | Shop name mismatch | Use one consistent name per shop in `SHOP_NAMES` and file names |

## Re-authorizing a shop

1. Run `python auth.py <shop>` on a machine with a browser and sign in as the shop owner.
2. Move the new `tokens_<shop>.json` to where the worker reads it (`TOKENS_DIR`, or `./tokens/` with Docker).
3. Run `python scheduler.py --once` to confirm.

## Database maintenance

```bash
# integrity check
python -c "import sqlite3; print(sqlite3.connect('data/shopwatch.db').execute('PRAGMA integrity_check').fetchall())"

# shrink the WAL file
python -c "import sqlite3; sqlite3.connect('data/shopwatch.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"

# backup
sqlite3 data/shopwatch.db ".backup data/backup.db"
```

## Keeping a bug log

When something breaks, write down what happened, what you saw, the cause and the fix. It makes the next incident faster and shows the project is maintained.
