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
