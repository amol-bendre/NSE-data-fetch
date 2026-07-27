"""
Generic, job-agnostic Telegram status reporter -- shared by every fetch
workflow in this repo (bhavcopy.yml, fii-derivatives.yml, and any future
fetcher). This script knows nothing about bhavcopies, FII reports, or any
other job-specific concept: it only knows how to turn a status word into a
message and send it. All job-specific logic (what counts as success vs.
partial vs. failure, and which detail to show) lives in each workflow's own
YAML, where it belongs.

Sends a message only on the last scheduled attempt for that job (controlled
entirely by the calling workflow's IS_LAST_ATTEMPT flag) -- earlier retries
never trigger anything from this script.

Reads:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID -- bot credentials
  IS_LAST_ATTEMPT   -- "true" only on the job's final scheduled attempt
  JOB_LABEL         -- e.g. "Bhavcopy", "FII Reports"
  STATUS            -- "success" | "partial" | "failure"
  DETAIL            -- freeform, only shown when STATUS == "partial"
  REPORT_DATE       -- the IST date the job was targeting

Wrapped in try/except throughout: a Telegram-side failure (bad token,
network issue, API outage) is logged and swallowed here, and never fails
the calling workflow's job.
"""
import os
import sys

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

IS_LAST_ATTEMPT = os.environ.get("IS_LAST_ATTEMPT", "false").strip().lower() == "true"
JOB_LABEL = os.environ.get("JOB_LABEL", "Job").strip()
STATUS = os.environ.get("STATUS", "").strip().lower()
DETAIL = os.environ.get("DETAIL", "").strip()
REPORT_DATE = os.environ.get("REPORT_DATE", "").strip() or "(date unknown)"


def build_message() -> str | None:
    if STATUS == "success":
        return f"✅ {JOB_LABEL} fetched for {REPORT_DATE}"
    if STATUS == "partial":
        suffix = f" — {DETAIL}" if DETAIL else ""
        return f"⚠️ {JOB_LABEL} partially fetched for {REPORT_DATE}{suffix}"
    if STATUS == "failure":
        return f"❌ {JOB_LABEL} NOT available for {REPORT_DATE} — all attempts failed"
    print(f"[telegram_alert] unrecognized STATUS={STATUS!r} -- not sending anything")
    return None


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

    message = build_message()
    if message is None:
        return

    print(f"[telegram_alert] last attempt -- sending: {message}")
    try:
        send_telegram_message(message)
        print("[telegram_alert] message sent successfully")
    except Exception as e:
        # Never let a Telegram-side problem fail the calling job.
        print(f"[telegram_alert] failed to send Telegram message: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
