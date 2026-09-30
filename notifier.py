import logging
import os
from typing import List, Optional
import requests
from dotenv import load_dotenv

from database import get_unsent_open_alerts, insert_alert, is_alert_open, mark_alert_sent
from models import Alert

load_dotenv()
logger = logging.getLogger("shopwatch.notifier")


def get_telegram_credentials() -> tuple[Optional[str], Optional[str]]:
    """Retrieve Telegram credentials from environment variables."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
    return token, chat_id


def escape_telegram_markdown(text: str) -> str:
    """Escape Telegram Markdown special characters in dynamic text bodies."""
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


def format_alert_markdown(alert: Alert) -> str:
    """Format an Alert object into a readable Telegram Markdown notification."""
    emoji = "🚨" if alert.severity == "CRITICAL" else ("⚠️" if alert.severity == "WARNING" else "ℹ️")
    created_str = alert.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    order_line = f"*Order:* `#{alert.order_id}`\n" if alert.order_id else ""
    escaped_msg = escape_telegram_markdown(alert.message)
    escaped_shop = escape_telegram_markdown(alert.shop_name)

    text = (
        f"{emoji} *[SHOPWATCH ALERT - {alert.severity}]*\n\n"
        f"*Shop:* {escaped_shop}\n"
        f"*Rule:* `{alert.rule_name}`\n"
        f"{order_line}"
        f"*Details:* {escaped_msg}\n"
        f"*Time:* {created_str}"
    )
    return text


def send_telegram_message(
    text: str,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """Send text payload to Telegram Bot API with Markdown formatting."""
    bot_token, default_chat_id = get_telegram_credentials()
    active_token = token or bot_token
    active_chat_id = chat_id or default_chat_id

    if not active_token or not active_chat_id:
        logger.info(
            "[Notifier] Telegram bot token or chat ID missing. Falling back to local logging."
        )
        return False

    url = f"https://api.telegram.org/bot{active_token}/sendMessage"
    payload = {
        "chat_id": active_chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code == 200:
            logger.info("[Notifier] Successfully dispatched Telegram alert.")
            return True
        else:
            logger.error(
                f"[Notifier] Telegram API returned HTTP {response.status_code}: {response.text}"
            )
            return False
    except requests.RequestException as exc:
        detail = str(exc).replace(active_token, "<redacted>")
        logger.error(f"[Notifier] Failed to connect to Telegram API: {detail}")
        return False


def dispatch_alert(alert: Alert, db_path: Optional[str] = None) -> bool:
    """
    Evaluate alert deduplication against database, save alert, and notify Telegram if enabled.
    Returns True if a new alert was created and dispatched, False if deduplicated.
    """
    # Fast-path deduplication check
    if is_alert_open(alert.rule_name, alert.order_id, alert.shop_name, db_path):
        logger.debug(
            f"[Notifier] Deduplicating existing open alert: rule={alert.rule_name}, order={alert.order_id}"
        )
        return False

    # Persist the alert to the database with atomic uniqueness
    alert_id = insert_alert(alert, db_path)
    if not alert_id:
        logger.debug(
            f"[Notifier] Concurrently deduplicated open alert: rule={alert.rule_name}, order={alert.order_id}"
        )
        return False

    alert.id = alert_id
    logger.warning(
        f"[ALERT GENERATED] [{alert.severity}] shop={alert.shop_name} rule={alert.rule_name}: {alert.message}"
    )

    # Attempt Telegram dispatch
    msg = format_alert_markdown(alert)
    dispatched = send_telegram_message(msg)
    if dispatched and alert_id:
        mark_alert_sent(alert_id, db_path)
        alert.sent_to_telegram = True

    return True



def dispatch_alerts(alerts: List[Alert], db_path: Optional[str] = None) -> int:
    """
    Dispatch a list of alerts, applying deduplication and saving each new alert.
    Returns count of newly generated alerts.
    """
    new_count = 0
    for a in alerts:
        if dispatch_alert(a, db_path):
            new_count += 1

    # Retry alerts whose Telegram delivery previously failed
    token, chat_id = get_telegram_credentials()
    for pending in (get_unsent_open_alerts(db_path) if token and chat_id else []):
        if send_telegram_message(format_alert_markdown(pending)):
            mark_alert_sent(pending.id, db_path)
    return new_count
