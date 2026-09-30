from datetime import datetime, timezone
import pytest
from unittest.mock import patch, MagicMock
from models import Alert
from notifier import format_alert_markdown, send_telegram_message, dispatch_alert, dispatch_alerts
from database import init_db, get_open_alerts


@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "notifier_test.db")
    init_db(db_file)
    return db_file


def test_format_alert_markdown():
    alert = Alert(
        order_id="12345",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Unshipped order test description",
        created_at=datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    md = format_alert_markdown(alert)
    assert "🚨 *[SHOPWATCH ALERT - CRITICAL]*" in md
    assert "*Shop:* ExampleShop" in md
    assert "*Order:* `#12345`" in md
    assert "late_unshipped" in md


def test_send_telegram_message_missing_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    success = send_telegram_message("Test message")
    assert success is False


@patch("requests.post")
def test_send_telegram_message_success(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock_chat_id")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    success = send_telegram_message("Test alert")
    assert success is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert "https://api.telegram.org/botmock_token/sendMessage" in args[0]
    assert kwargs["json"]["chat_id"] == "mock_chat_id"


def test_dispatch_alert_deduplication(temp_db, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    alert = Alert(
        order_id="999",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Order is late",
    )

    # First dispatch should succeed and record to DB
    dispatched_1 = dispatch_alert(alert, temp_db)
    assert dispatched_1 is True

    # Immediate second dispatch of the exact same problem should be deduplicated
    alert_duplicate = Alert(
        order_id="999",
        shop_name="ExampleShop",
        rule_name="late_unshipped",
        severity="CRITICAL",
        message="Order is late again",
    )
    dispatched_2 = dispatch_alert(alert_duplicate, temp_db)
    assert dispatched_2 is False

    open_alerts = get_open_alerts(temp_db)
    assert len(open_alerts) == 1


def test_format_alert_markdown_escaping():
    alert = Alert(
        order_id="ORD_123_ABC",
        shop_name="Avia_Spark",
        rule_name="buyer_velocity",
        severity="WARNING",
        message="Buyer user_name_456 placed *urgent* orders [batch_1]",
    )
    md = format_alert_markdown(alert)
    assert "user\\_name\\_456" in md
    assert "Avia\\_Spark" in md
    assert "\\[batch\\_1]" in md

