import shutil
from pathlib import Path

from database import get_sync_stats
from scheduler import get_shop_names, run_sync_cycle

FIXTURE = Path(__file__).parent / "fixtures" / "sample_orders.csv"


def test_shop_names_come_from_env(monkeypatch):
    assert get_shop_names() == ["myshop"]
    monkeypatch.setenv("SHOP_NAMES", "alpha, beta ,")
    assert get_shop_names() == ["alpha", "beta"]


def test_each_shop_loads_its_own_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_NAMES", "alpha,beta")
    shutil.copy(FIXTURE, tmp_path / "orders_alpha.csv")
    shutil.copy(FIXTURE, tmp_path / "orders_beta.csv")
    db = str(tmp_path / "multi.db")

    run = run_sync_cycle(db_path=db)

    assert run.ok is True
    stats = get_sync_stats(db)
    assert stats["orders_by_shop"] == {"alpha": 2, "beta": 2}


def test_shop_without_data_does_not_fail_the_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_NAMES", "alpha,beta")
    shutil.copy(FIXTURE, tmp_path / "orders_alpha.csv")
    db = str(tmp_path / "partial.db")

    assert run_sync_cycle(db_path=db).ok is True
    assert get_sync_stats(db)["orders_by_shop"] == {"alpha": 2}


class FakeEtsy:
    calls = []

    def __init__(self, shop):
        self.shop = shop

    def receipts(self, min_last_modified=None):
        FakeEtsy.calls.append(min_last_modified)
        return [{"receipt_id": 501, "created_timestamp": 1790683800, "is_paid": True}]


def _use_fake_etsy(tmp_path, monkeypatch):
    import etsy

    FakeEtsy.calls = []
    monkeypatch.setattr(etsy, "Etsy", FakeEtsy)
    monkeypatch.setenv("ETSY_KEYSTRING", "test-key")
    monkeypatch.setenv("SHOP_NAMES", "alpha")
    (tmp_path / "tokens_alpha.json").write_text("{}", encoding="utf-8")


def test_first_api_sync_is_full_then_incremental(tmp_path, monkeypatch):
    from database import get_last_api_sync

    _use_fake_etsy(tmp_path, monkeypatch)
    db = str(tmp_path / "inc.db")

    run_sync_cycle(db_path=db)
    first_sync = get_last_api_sync("alpha", db)
    run_sync_cycle(db_path=db)

    assert FakeEtsy.calls[0] is None
    assert first_sync is not None
    # second call asks only for changes since the first sync, minus the safety overlap
    assert FakeEtsy.calls[1] == (first_sync.timestamp() - 3600)


def test_full_sync_ignores_saved_sync_point(tmp_path, monkeypatch):
    _use_fake_etsy(tmp_path, monkeypatch)
    db = str(tmp_path / "full.db")

    run_sync_cycle(db_path=db)
    run_sync_cycle(db_path=db, full_sync=True)

    assert FakeEtsy.calls == [None, None]


def test_csv_is_not_used_when_api_returns_no_changes(tmp_path, monkeypatch):
    _use_fake_etsy(tmp_path, monkeypatch)
    monkeypatch.setattr(FakeEtsy, "receipts", lambda self, min_last_modified=None: [])
    shutil.copy(FIXTURE, tmp_path / "orders_alpha.csv")
    db = str(tmp_path / "nocsv.db")

    run_sync_cycle(db_path=db)

    assert get_sync_stats(db)["total_orders"] == 0
