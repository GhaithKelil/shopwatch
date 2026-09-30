import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Keep tests off the network and away from real tokens, CSVs and databases."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ETSY_KEYSTRING", "")
    monkeypatch.setenv("TOKENS_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.delenv("CSV_PATH", raising=False)
    monkeypatch.delenv("SHOP_NAMES", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "shopwatch.db"))
