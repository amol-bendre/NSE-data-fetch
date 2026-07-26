"""
Sends a single Telegram message per weekday about the bhavcopy fetch job --
and only on the last scheduled attempt (11:32 PM IST), regardless of how
that attempt turned out. Earlier attempts (10:07 PM, 10:37 PM IST) never
trigger any message from this script.

Fully self-contained and independent of fetch_bhavcopy.py:
  - Reads two flags set by the workflow YAML (not by fetch_bhavcopy.py):
      IS_LAST_ATTEMPT -- "true" only on the 11:32 PM IST scheduled run
      BHAV_AVAILABLE  -- "true"/"false", from fetch_bhavcopy.py's own
                         GITHUB_OUTPUT (read by the workflow step and
                         passed through as an env var here)
      BHAV_DATE       -- the IST date the job was targeting, also from
                         fetch_bhavcopy.py's GITHUB_OUTPUT
  - Owns the Telegram bot token, chat ID, and all message text -- nothing
    else in this project touches Telegram. Changing the bot, chat, or
    wording later means editing only this file.
  - Wrapped in try/except: any Telegram-side failure (bad token, network
    issue, Telegram API outage) is logged and swallowed here, and never
    fails the workflow job.

Runs as a second step in bhavcopy.yml with `if: always()`, so it still
executes even if the fetch_bhavcopy.py step itself failed or crashed.
"""
import os
import sys

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

IS_LAST_ATTEMPT = os.environ.get("IS_LAST_ATTEMPT", "false").strip().lower() == "true"
BHAV_AVAILABLE = os.environ.get("BHAV_AVAILABLE", "false").strip().lower() == "true"
BHAV_DATE = os.environ.get("BHAV_DATE", "").strip()


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()


def main():
    if not IS_LAST_ATTEMPT:
        print("[telegram_alert] not the last scheduled attempt -- staying silent")
        return

    if not BOT_TOKEN or not CHAT_ID:
        print("[telegram_alert] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- skipping alert")
        return

    date_str = BHAV_DATE or "(date unknown)"
    if BHAV_AVAILABLE:
        message = f"✅ Bhavcopy fetched for {date_str}"
    else:
        message = f"⚠️ Bhavcopy NOT available for {date_str} — all 3 attempts failed"

    print(f"[telegram_alert] last attempt -- sending: {message}")
    try:
        send_telegram_message(message)
        print("[telegram_alert] message sent successfully")
    except Exception as e:
        # Never let a Telegram-side problem fail the job.
        print(f"[telegram_alert] failed to send Telegram message: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
